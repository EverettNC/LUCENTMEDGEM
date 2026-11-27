// lucent_dsp.hpp — LUCENT: The Cornea of The Christman AI Project
// Sister organ to CORTI. Protocol: TCAP-V-0044. Zero external dependencies.
// Rule: It sees, or it says it did not lock. Never invents a value over dead input.

#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <iomanip>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

namespace lucent {

constexpr float kRadToDeg = 180.0f / 3.14159265358979323846f;

enum class SensorState {
    LOCKED,
    NO_SIGNAL,
    DARK_OCCLUDED,
    WASHED,
    FROZEN
};

inline const char* to_string(SensorState s) {
    switch (s) {
        case SensorState::LOCKED:        return "LOCKED";
        case SensorState::NO_SIGNAL:     return "NO_SIGNAL";
        case SensorState::DARK_OCCLUDED: return "DARK_OCCLUDED";
        case SensorState::WASHED:        return "WASHED";
        case SensorState::FROZEN:        return "FROZEN";
    }
    return "NO_SIGNAL";
}

enum class SightKind { BLINK, SHIFT, SWEEP, STILL, GLARE, NOISE };

inline const char* to_string(SightKind k) {
    switch (k) {
        case SightKind::BLINK: return "blink";
        case SightKind::SHIFT: return "shift";
        case SightKind::SWEEP: return "sweep";
        case SightKind::STILL: return "still";
        case SightKind::GLARE: return "glare";
        case SightKind::NOISE: return "noise";
    }
    return "noise";
}

struct SightFrame {
    uint64_t t_ms = 0;
    SensorState state = SensorState::NO_SIGNAL;
    float luma = 0.0f;
    float contrast = 0.0f;
    float edge_energy = 0.0f;
    std::optional<float> motion_mag;
    std::optional<float> motion_dir_deg;
    std::optional<float> centroid_x;
    std::optional<float> centroid_y;
    std::optional<float> dominant_angle_deg;
};

struct SightEvent {
    uint64_t id = 0;
    SightKind kind = SightKind::NOISE;
    uint64_t started_at_ms = 0;
    uint64_t duration_ms = 0;
    float peak_motion = 0.0f;
    float mean_luma = 0.0f;
    float mean_edge = 0.0f;
    std::optional<float> median_dir_deg;
    bool locked = false;
    std::vector<SightFrame> tape;

    std::string to_json() const {
        auto opt = [](const std::optional<float>& v) -> std::string {
            if (!v.has_value()) return "null";
            std::ostringstream s;
            s << std::fixed << std::setprecision(4) << *v;
            return s.str();
        };
        std::ostringstream o;
        o << std::fixed << std::setprecision(4)
          << "{"
          << "\"organ\":\"LUCENT\","
          << "\"protocol\":\"TCAP-V-0044\","
          << "\"execution_locality\":\"on_device\","
          << "\"left_machine\":false,"
          << "\"id\":" << id << ","
          << "\"kind\":\"" << to_string(kind) << "\","
          << "\"started_at_ms\":" << started_at_ms << ","
          << "\"duration_ms\":" << duration_ms << ","
          << "\"peak_motion\":" << peak_motion << ","
          << "\"mean_luma\":" << mean_luma << ","
          << "\"mean_edge\":" << mean_edge << ","
          << "\"median_dir_deg\":" << opt(median_dir_deg) << ","
          << "\"locked\":" << (locked ? "true" : "false") << ","
          << "\"tape_frames\":" << tape.size()
          << "}";
        return o.str();
    }
};

struct RoomSeat {
    bool seated = false;
    float baseline_luma = 0.0f;
    float baseline_edge = 0.0f;
    float noise_floor_motion = 0.0f;
};

class Cornea {
public:
    static constexpr size_t   kTapeCap          = 360;
    static constexpr size_t   kRingCap          = 8;
    static constexpr float    kCoherenceLock    = 0.62f;
    static constexpr float    kMotionOnset      = 0.018f;
    static constexpr float    kMotionOffset     = 0.007f;
    static constexpr uint64_t kMinEventMs       = 60;
    static constexpr int      kFrozenFrameLimit = 15;

    SightFrame ingest(const uint8_t* pixels, int width, int height, int channels,
                      uint64_t timestamp_ms,
                      std::optional<SightEvent>& closed_out) {
        closed_out.reset();
        SightFrame f;
        f.t_ms = timestamp_ms;

        if (!pixels || width < 4 || height < 4 || (channels != 1 && channels != 3)) {
            f.state = SensorState::NO_SIGNAL;
            return f;
        }

        const size_t n = static_cast<size_t>(width) * static_cast<size_t>(height);
        std::vector<float> curr(n);
        double sum = 0.0, sum_sq = 0.0;
        size_t dark_px = 0, bright_px = 0;

        for (size_t i = 0; i < n; ++i) {
            float y = 0.0f;
            if (channels == 1) {
                y = pixels[i] / 255.0f;
            } else {
                const size_t idx = i * 3;
                y = (0.2126f * pixels[idx] +
                     0.7152f * pixels[idx + 1] +
                     0.0722f * pixels[idx + 2]) / 255.0f;
            }
            curr[i] = y;
            sum += y;
            sum_sq += static_cast<double>(y) * y;
            if (y < 0.02f) ++dark_px;
            if (y > 0.98f) ++bright_px;
        }

        f.luma = static_cast<float>(sum / static_cast<double>(n));
        double var = std::max(0.0, (sum_sq / static_cast<double>(n)) -
                                   static_cast<double>(f.luma) * f.luma);
        f.contrast = static_cast<float>(std::sqrt(var));

        const float dark_ratio   = static_cast<float>(dark_px)   / static_cast<float>(n);
        const float bright_ratio = static_cast<float>(bright_px) / static_cast<float>(n);

        if (dark_ratio > 0.96f || (f.luma < 0.02f && f.contrast < 0.01f)) {
            f.state = SensorState::DARK_OCCLUDED;
            prev_luma_.clear();
            frozen_streak_ = 0;
            return f;
        }
        if (bright_ratio > 0.96f || (f.luma > 0.98f && f.contrast < 0.01f)) {
            f.state = SensorState::WASHED;
            prev_luma_.clear();
            frozen_streak_ = 0;
            return f;
        }

        double edge_sum = 0.0;
        double e0 = 0.0, e45 = 0.0, e90 = 0.0, e135 = 0.0;
        size_t samples = 0;

        for (int y = 1; y < height - 1; y += 2) {
            for (int x = 1; x < width - 1; x += 2) {
                auto at = [&](int dx, int dy) {
                    return curr[static_cast<size_t>(y + dy) * width + (x + dx)];
                };
                float gx = -at(-1,-1) + at(1,-1) - 2.0f*at(-1,0) + 2.0f*at(1,0) - at(-1,1) + at(1,1);
                float gy = -at(-1,-1) - 2.0f*at(0,-1) - at(1,-1) + at(-1,1) + 2.0f*at(0,1) + at(1,1);
                float mag = std::hypot(gx, gy) * 0.25f;
                edge_sum += mag;
                e0   += std::abs(gy);
                e90  += std::abs(gx);
                e45  += std::abs(gx + gy) * 0.7071f;
                e135 += std::abs(gx - gy) * 0.7071f;
                ++samples;
            }
        }
        f.edge_energy = samples ? static_cast<float>(edge_sum / samples) : 0.0f;

        double max_e = std::max({e0, e45, e90, e135});
        double avg_e = (e0 + e45 + e90 + e135) * 0.25;
        if (max_e > avg_e * 1.18 && f.edge_energy > 0.015f) {
            if (max_e == e0)        f.dominant_angle_deg = 0.0f;
            else if (max_e == e45)  f.dominant_angle_deg = 45.0f;
            else if (max_e == e90)  f.dominant_angle_deg = 90.0f;
            else                    f.dominant_angle_deg = 135.0f;
        }

        if (prev_luma_.size() == n && prev_w_ == width && prev_h_ == height) {
            float max_diff = 0.0f;
            double diff_sum = 0.0;
            double sum_vx = 0.0, sum_vy = 0.0, sum_vmag = 0.0;
            double cx_num = 0.0, cy_num = 0.0, c_den = 0.0;

            for (int y = 1; y < height - 1; y += 2) {
                for (int x = 1; x < width - 1; x += 2) {
                    const size_t idx = static_cast<size_t>(y) * width + x;
                    float it = curr[idx] - prev_luma_[idx];
                    float ad = std::abs(it);
                    if (ad > max_diff) max_diff = ad;
                    diff_sum += ad;

                    if (ad > 0.012f) {
                        float ix = 0.5f * (curr[idx + 1] - curr[idx - 1]);
                        float iy = 0.5f * (curr[idx + width] - curr[idx - width]);
                        float denom = ix * ix + iy * iy + 1e-4f;
                        float vx = -it * ix / denom;
                        float vy = -it * iy / denom;
                        float vm = std::hypot(vx, vy);
                        sum_vx += vx;
                        sum_vy += vy;
                        sum_vmag += vm;

                        cx_num += static_cast<double>(x) * ad;
                        cy_num += static_cast<double>(y) * ad;
                        c_den  += ad;
                    }
                }
            }

            if (max_diff < 1e-6f) {
                ++frozen_streak_;
                if (frozen_streak_ >= kFrozenFrameLimit) {
                    f.state = SensorState::FROZEN;
                    return f;
                }
            } else {
                frozen_streak_ = 0;
            }

            float mean_motion = samples ? static_cast<float>(diff_sum / samples) : 0.0f;
            float coherence   = (sum_vmag > 1e-5)
                                ? static_cast<float>(std::hypot(sum_vx, sum_vy) / sum_vmag)
                                : 0.0f;

            if (mean_motion >= kMotionOffset && coherence >= kCoherenceLock && c_den > 0.0) {
                f.motion_mag     = mean_motion;
                f.motion_dir_deg = static_cast<float>(std::atan2(sum_vy, sum_vx)) * kRadToDeg;
                f.centroid_x     = static_cast<float>((cx_num / c_den) / width);
                f.centroid_y     = static_cast<float>((cy_num / c_den) / height);
            }
        }

        f.state = SensorState::LOCKED;
        prev_luma_ = std::move(curr);
        prev_w_ = width;
        prev_h_ = height;

        track_event(f, timestamp_ms, closed_out);
        return f;
    }

    bool seat_room(const std::vector<SightFrame>& samples) {
        std::vector<const SightFrame*> valid;
        for (const auto& s : samples) {
            if (s.state == SensorState::LOCKED) valid.push_back(&s);
        }
        if (valid.size() < 3) return false;

        float l = 0.0f, e = 0.0f, m = 0.0f;
        for (const auto* s : valid) {
            l += s->luma;
            e += s->edge_energy;
            m += s->motion_mag.value_or(0.0f);
        }
        seat_.seated             = true;
        seat_.baseline_luma      = l / valid.size();
        seat_.baseline_edge      = e / valid.size();
        seat_.noise_floor_motion = m / valid.size();
        return true;
    }

    const RoomSeat& seat() const { return seat_; }
    const std::deque<SightEvent>& ring() const { return ring_; }

private:
    void track_event(const SightFrame& live, uint64_t ts_ms,
                     std::optional<SightEvent>& closed_out) {
        const float m = live.motion_mag.value_or(0.0f);
        const float onset  = std::max(kMotionOnset,  seat_.seated ? seat_.noise_floor_motion * 2.5f : 0.0f);
        const float offset = std::max(kMotionOffset, seat_.seated ? seat_.noise_floor_motion * 1.3f : 0.0f);

        if (!in_event_) {
            if (m >= onset) {
                in_event_ = true;
                active_ = SightEvent{};
                active_.id = ++next_id_;
                active_.started_at_ms = ts_ms;
                push_tape(live, ts_ms);
            }
            return;
        }

        push_tape(live, ts_ms);
        if (m < offset) {
            uint64_t dur = ts_ms - active_.started_at_ms;
            if (dur >= kMinEventMs) {
                finalize_active(dur);
                ring_.push_front(active_);
                if (ring_.size() > kRingCap) ring_.pop_back();
                closed_out = active_;
            }
            in_event_ = false;
        }
    }

    void push_tape(const SightFrame& live, uint64_t ts_ms) {
        if (active_.tape.size() < kTapeCap) {
            SightFrame entry = live;
            entry.t_ms = ts_ms - active_.started_at_ms;
            active_.tape.push_back(entry);
        }
    }

    void finalize_active(uint64_t dur_ms) {
        active_.duration_ms = dur_ms;
        std::vector<float> dirs;
        double l_sum = 0.0, e_sum = 0.0;
        for (const auto& fr : active_.tape) {
            l_sum += fr.luma;
            e_sum += fr.edge_energy;
            if (fr.motion_mag.has_value()) {
                active_.peak_motion = std::max(active_.peak_motion, *fr.motion_mag);
            }
            if (fr.motion_dir_deg.has_value()) {
                dirs.push_back(*fr.motion_dir_deg);
            }
        }
        const double cnt = std::max<size_t>(1, active_.tape.size());
        active_.mean_luma = static_cast<float>(l_sum / cnt);
        active_.mean_edge = static_cast<float>(e_sum / cnt);
        active_.locked    = dirs.size() >= 2;
        if (active_.locked) {
            std::sort(dirs.begin(), dirs.end());
            active_.median_dir_deg = dirs[dirs.size() / 2];
        }

        if (!active_.locked)                  active_.kind = SightKind::NOISE;
        else if (dur_ms < 220)                active_.kind = SightKind::BLINK;
        else if (active_.peak_motion > 0.08f) active_.kind = SightKind::SWEEP;
        else                                  active_.kind = SightKind::SHIFT;
    }

    std::vector<float> prev_luma_;
    int prev_w_ = 0, prev_h_ = 0;
    int frozen_streak_ = 0;
    bool in_event_ = false;
    uint64_t next_id_ = 0;
    SightEvent active_;
    std::deque<SightEvent> ring_;
    RoomSeat seat_;
};

} // namespace lucent
