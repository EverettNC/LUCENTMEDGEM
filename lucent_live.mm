// lucent_live.mm — LUCENT Live macOS Cornea Runner (with native CoreImage JPEG capture)
// Native AVFoundation + CoreVideo + CoreImage + ImageIO + CommonCrypto. Zero external deps.
// Protocol: TCAP-V-0044 | Locality: on_device only.

#import <AVFoundation/AVFoundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreImage/CoreImage.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <Foundation/Foundation.h>
#import <ImageIO/ImageIO.h>

#include <CommonCrypto/CommonDigest.h>
#include <arpa/inet.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "lucent_dsp.hpp"

namespace {

std::atomic<bool> g_running{true};
std::atomic<bool> g_seat_requested{false};
std::atomic<bool> g_look_requested{false};

void handle_signal(int sig) {
    if (sig == SIGUSR1) {
        g_seat_requested.store(true);
    } else if (sig == SIGUSR2) {
        g_look_requested.store(true);
    } else {
        g_running.store(false);
    }
}

uint64_t now_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

std::string sha256_hex(const uint8_t* data, size_t len) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data, static_cast<CC_LONG>(len), digest);
    std::ostringstream oss;
    oss << std::hex << std::setfill('0');
    for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; ++i) {
        oss << std::setw(2) << static_cast<int>(digest[i]);
    }
    return oss.str();
}

std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        if (c == '"')       out += "\\\"";
        else if (c == '\\') out += "\\\\";
        else if (c == '\n') out += "\\n";
        else                out += c;
    }
    return out;
}

struct LoopbackTarget {
    bool enabled = false;
    uint16_t port = 0;
    std::string path = "/";
};

bool parse_loopback_url(const std::string& url, LoopbackTarget& out, std::string& err) {
    const std::string prefix = "http://";
    if (url.rfind(prefix, 0) != 0) {
        err = "only http://127.0.0.1:<port>/... or http://localhost:<port>/... is allowed";
        return false;
    }
    std::string rest = url.substr(prefix.size());
    size_t slash = rest.find('/');
    std::string hostport = (slash == std::string::npos) ? rest : rest.substr(0, slash);
    std::string path     = (slash == std::string::npos) ? "/"  : rest.substr(slash);

    size_t colon = hostport.find(':');
    if (colon == std::string::npos) {
        err = "explicit port required (e.g. http://127.0.0.1:8790/lucent)";
        return false;
    }
    std::string host = hostport.substr(0, colon);
    std::string pstr = hostport.substr(colon + 1);
    if (host != "127.0.0.1" && host != "localhost") {
        err = "refused non-loopback host '" + host + "' — LUCENT never sends frames off-device";
        return false;
    }
    int port_val = std::atoi(pstr.c_str());
    if (port_val < 1 || port_val > 65535) {
        err = "invalid port: " + pstr;
        return false;
    }
    out.enabled = true;
    out.port = static_cast<uint16_t>(port_val);
    out.path = path;
    return true;
}

bool post_loopback_json(const LoopbackTarget& target, const std::string& json_body, std::string& err) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { err = "socket() failed"; return false; }
    struct timeval tv{2, 0};
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(target.port);
    ::inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        err = std::string("connect 127.0.0.1:") + std::to_string(target.port) + " failed: " + std::strerror(errno);
        ::close(fd);
        return false;
    }
    std::ostringstream req;
    req << "POST " << target.path << " HTTP/1.1\r\n"
        << "Host: 127.0.0.1:" << target.port << "\r\n"
        << "Content-Type: application/json\r\n"
        << "Content-Length: " << json_body.size() << "\r\n"
        << "Connection: close\r\n\r\n"
        << json_body;
    std::string wire = req.str();
    size_t sent = 0;
    while (sent < wire.size()) {
        ssize_t n = ::send(fd, wire.data() + sent, wire.size() - sent, 0);
        if (n <= 0) { err = "send() failed"; ::close(fd); return false; }
        sent += static_cast<size_t>(n);
    }
    char resp[256];
    ssize_t r = ::recv(fd, resp, sizeof(resp) - 1, 0);
    ::close(fd);
    if (r <= 0) { err = "no HTTP reply from receiver"; return false; }
    resp[r] = '\0';
    if (!std::strstr(resp, " 200 ") && !std::strstr(resp, " 201 ") &&
        !std::strstr(resp, " 202 ") && !std::strstr(resp, " 204 ")) {
        err = std::string("receiver rejected event: ") + std::string(resp, strcspn(resp, "\r\n"));
        return false;
    }
    return true;
}

std::string enrich_event_json(const std::string& base_json,
                              const std::string& frame_hash,
                              const std::string& frame_path,
                              bool seated,
                              const std::string& trigger) {
    std::string base = base_json;
    if (!base.empty() && base.back() == '}') {
        base.pop_back();
        base += ",\"trigger\":\"" + json_escape(trigger) + "\"";
        base += ",\"frame_sha256\":\"" + frame_hash + "\"";
        if (!frame_path.empty()) {
            base += ",\"frame_path\":\"" + json_escape(frame_path) + "\"";
        }
        base += ",\"seat_status\":\"" + std::string(seated ? "SEATED" : "COLD") + "\"";
        base += "}";
    }
    return base;
}

} // namespace

@interface LucentDelegate : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>
@property (nonatomic, assign) lucent::Cornea* cornea;
@property (nonatomic, assign) std::mutex* lock;
@property (nonatomic, assign) std::deque<lucent::SightFrame>* recentLocked;
@property (nonatomic, assign) std::atomic<uint64_t>* lastFrameMs;
@property (nonatomic, assign) LoopbackTarget postTarget;
@property (nonatomic, assign) bool verboseFrames;
@property (nonatomic, copy) NSString* framesDir;
@property (nonatomic, strong) CIContext* ciContext;
@end

@implementation LucentDelegate

- (std::pair<std::string, std::string>)saveVerifiedJPEG:(CVImageBufferRef)pb tag:(NSString*)tag {
    if (!self.framesDir || self.framesDir.length == 0) return {"", ""};
    CIImage* ci = [CIImage imageWithCVPixelBuffer:pb];
    if (!ci) return {"", ""};

    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
    NSDictionary* opts = @{(id)kCGImageDestinationLossyCompressionQuality: @(0.85)};
    NSData* jpegData = [self.ciContext JPEGRepresentationOfImage:ci colorSpace:cs options:opts];
    CGColorSpaceRelease(cs);
    if (!jpegData || jpegData.length == 0) return {"", ""};

    NSString* fullPath = [self.framesDir stringByAppendingPathComponent:
                          [NSString stringWithFormat:@"%@.jpg", tag]];
    if (![jpegData writeToFile:fullPath atomically:YES]) return {"", ""};

    std::string hash = sha256_hex(static_cast<const uint8_t*>(jpegData.bytes), jpegData.length);
    return {std::string(fullPath.UTF8String), hash};
}

- (void)captureOutput:(AVCaptureOutput*)output
didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
       fromConnection:(AVCaptureConnection*)connection {
    (void)output;
    (void)connection;
    if (!g_running.load()) return;

    @autoreleasepool {
        CVImageBufferRef pb = CMSampleBufferGetImageBuffer(sampleBuffer);
        if (!pb) return;

        if (CVPixelBufferLockBaseAddress(pb, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) {
            return;
        }

        const bool planar = CVPixelBufferIsPlanar(pb);
        const size_t src_w = planar ? CVPixelBufferGetWidthOfPlane(pb, 0) : CVPixelBufferGetWidth(pb);
        const size_t src_h = planar ? CVPixelBufferGetHeightOfPlane(pb, 0) : CVPixelBufferGetHeight(pb);
        const size_t stride = planar ? CVPixelBufferGetBytesPerRowOfPlane(pb, 0) : CVPixelBufferGetBytesPerRow(pb);
        const uint8_t* base = static_cast<const uint8_t*>(
            planar ? CVPixelBufferGetBaseAddressOfPlane(pb, 0) : CVPixelBufferGetBaseAddress(pb)
        );

        std::vector<uint8_t> gray;
        int dst_w = 0, dst_h = 0;

        if (base && src_w >= 4 && src_h >= 4) {
            constexpr int kTargetMaxW = 320;
            dst_w = (static_cast<int>(src_w) > kTargetMaxW) ? kTargetMaxW : static_cast<int>(src_w);
            dst_h = std::max(4, static_cast<int>((src_h * dst_w) / src_w));
            gray.resize(static_cast<size_t>(dst_w) * static_cast<size_t>(dst_h));

            for (int y = 0; y < dst_h; ++y) {
                const size_t sy = (static_cast<size_t>(y) * src_h) / dst_h;
                const uint8_t* row = base + sy * stride;
                for (int x = 0; x < dst_w; ++x) {
                    const size_t sx = (static_cast<size_t>(x) * src_w) / dst_w;
                    gray[static_cast<size_t>(y) * dst_w + x] = row[sx];
                }
            }
        }

        CVPixelBufferUnlockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);

        const uint64_t ts = now_ms();
        self.lastFrameMs->store(ts);

        std::optional<lucent::SightEvent> closed;
        lucent::SightFrame live;
        bool seated = false;

        {
            std::lock_guard<std::mutex> guard(*self.lock);
            live = self.cornea->ingest(gray.empty() ? nullptr : gray.data(),
                                       dst_w, dst_h, 1, ts, closed);
            if (live.state == lucent::SensorState::LOCKED) {
                self.recentLocked->push_back(live);
                if (self.recentLocked->size() > 8) self.recentLocked->pop_front();
            }
            if (g_seat_requested.exchange(false)) {
                std::vector<lucent::SightFrame> samples(self.recentLocked->begin(),
                                                        self.recentLocked->end());
                if (self.cornea->seat_room(samples)) {
                    const auto& st = self.cornea->seat();
                    std::fprintf(stderr,
                        "{\"organ\":\"LUCENT\",\"seat_status\":\"SEATED\","
                        "\"baseline_luma\":%.4f,\"baseline_edge\":%.4f,\"noise_floor\":%.4f}\n",
                        st.baseline_luma, st.baseline_edge, st.noise_floor_motion);
                } else {
                    std::fprintf(stderr,
                        "{\"organ\":\"LUCENT\",\"seat_status\":\"REFUSED\","
                        "\"reason\":\"need at least 3 LOCKED frames to seat room\"}\n");
                }
                std::fflush(stderr);
            }
            seated = self.cornea->seat().seated;
        }

        const bool want_manual = g_look_requested.exchange(false);

        if (live.state != lucent::SensorState::LOCKED) {
            static lucent::SensorState last_fault = lucent::SensorState::LOCKED;
            if (live.state != last_fault || want_manual) {
                std::fprintf(stderr,
                    "{\"organ\":\"LUCENT\",\"protocol\":\"TCAP-V-0044\","
                    "\"sensor_state\":\"%s\",\"t_ms\":%llu,\"output_dropped\":true%s}\n",
                    lucent::to_string(live.state),
                    static_cast<unsigned long long>(ts),
                    want_manual ? ",\"manual_look\":\"refused\"" : "");
                std::fflush(stderr);
                last_fault = live.state;
            }
            return;
        }

        if (self.verboseFrames) {
            std::fprintf(stderr,
                "[LUCENT] state=LOCKED luma=%.3f contrast=%.3f edge=%.3f motion=%s dir=%s\n",
                live.luma, live.contrast, live.edge_energy,
                live.motion_mag ? std::to_string(*live.motion_mag).substr(0, 6).c_str() : "null",
                live.motion_dir_deg ? std::to_string(*live.motion_dir_deg).substr(0, 6).c_str() : "null");
            std::fflush(stderr);
        }

        auto emit_payload = [&](const std::string& base_json, NSString* tag, const std::string& trigger) {
            auto [fpath, fhash] = [self saveVerifiedJPEG:pb tag:tag];
            if (fhash.empty()) {
                fhash = sha256_hex(gray.data(), gray.size());
            }
            std::string payload = enrich_event_json(base_json, fhash, fpath, seated, trigger);
            std::printf("%s\n", payload.c_str());
            std::fflush(stdout);

            if (self.postTarget.enabled) {
                std::string post_err;
                if (!post_loopback_json(self.postTarget, payload, post_err)) {
                    std::fprintf(stderr,
                        "{\"organ\":\"LUCENT\",\"post_error\":\"%s\"}\n", post_err.c_str());
                    std::fflush(stderr);
                }
            }
        };

        if (closed.has_value()) {
            NSString* tag = [NSString stringWithFormat:@"event_%llu",
                             static_cast<unsigned long long>(closed->id)];
            emit_payload(closed->to_json(), tag, "motion_event");
        } else if (want_manual) {
            std::ostringstream o;
            o << std::fixed << std::setprecision(4)
              << "{"
              << "\"organ\":\"LUCENT\","
              << "\"protocol\":\"TCAP-V-0044\","
              << "\"execution_locality\":\"on_device\","
              << "\"left_machine\":false,"
              << "\"id\":" << ts << ","
              << "\"kind\":\"manual_look\","
              << "\"started_at_ms\":" << ts << ","
              << "\"duration_ms\":0,"
              << "\"mean_luma\":" << live.luma << ","
              << "\"contrast\":" << live.contrast << ","
              << "\"mean_edge\":" << live.edge_energy << ","
              << "\"locked\":true"
              << "}";
            NSString* tag = [NSString stringWithFormat:@"look_%llu",
                             static_cast<unsigned long long>(ts)];
            emit_payload(o.str(), tag, "manual_look");
        }
    }
}

@end

int main(int argc, char** argv) {
    LoopbackTarget post_target;
    bool verbose = false;
    std::string frames_dir = "";

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--verbose" || arg == "-v") {
            verbose = true;
        } else if (arg == "--save-frames" && i + 1 < argc) {
            frames_dir = argv[++i];
        } else if (arg == "--post" && i + 1 < argc) {
            std::string err;
            if (!parse_loopback_url(argv[++i], post_target, err)) {
                std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"status\":\"refused\",\"error\":\"%s\"}\n", err.c_str());
                return 2;
            }
        } else if (arg == "--help" || arg == "-h") {
            std::fprintf(stderr,
                "Usage: lucent_live [--verbose] [--save-frames DIR] [--post http://127.0.0.1:<port>/path]\n"
                "Keys:  v + Enter = instant look | s + Enter = seat room | q + Enter = quit\n");
            return 0;
        }
    }

    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    std::signal(SIGUSR1, handle_signal);
    std::signal(SIGUSR2, handle_signal);

    @autoreleasepool {
        AVAuthorizationStatus auth = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
        if (auth == AVAuthorizationStatusDenied) {
            std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"camera_error\":\"DENIED\","
                                 "\"detail\":\"Grant Camera permission to Terminal in System Settings > Privacy & Security > Camera\"}\n");
            return 1;
        }
        if (auth == AVAuthorizationStatusRestricted) {
            std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"camera_error\":\"RESTRICTED\"}\n");
            return 1;
        }
        if (auth == AVAuthorizationStatusNotDetermined) {
            dispatch_semaphore_t sem = dispatch_semaphore_create(0);
            __block BOOL granted = NO;
            [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL ok) {
                granted = ok;
                dispatch_semaphore_signal(sem);
            }];
            dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);
            if (!granted) {
                std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"camera_error\":\"DENIED\"}\n");
                return 1;
            }
        }

        AVCaptureDevice* device = [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo];
        if (!device) {
            std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"camera_error\":\"MISSING\"}\n");
            return 1;
        }

        NSError* error = nil;
        AVCaptureDeviceInput* input = [AVCaptureDeviceInput deviceInputWithDevice:device error:&error];
        if (!input || error) {
            std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"camera_error\":\"BUSY\",\"detail\":\"%s\"}\n",
                         error ? error.localizedDescription.UTF8String : "could not open camera");
            return 1;
        }

        NSString* nsFramesDir = nil;
        if (!frames_dir.empty()) {
            nsFramesDir = [[NSString stringWithUTF8String:frames_dir.c_str()] stringByExpandingTildeInPath];
            [[NSFileManager defaultManager] createDirectoryAtPath:nsFramesDir
                                      withIntermediateDirectories:YES
                                                       attributes:nil
                                                            error:nil];
        }

        AVCaptureSession* session = [[AVCaptureSession alloc] init];
        [session beginConfiguration];
        if ([session canSetSessionPreset:AVCaptureSessionPreset640x480]) {
            session.sessionPreset = AVCaptureSessionPreset640x480;
        }
        if (![session canAddInput:input]) {
            std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"camera_error\":\"SESSION_INPUT_FAILED\"}\n");
            return 1;
        }
        [session addInput:input];

        AVCaptureVideoDataOutput* output = [[AVCaptureVideoDataOutput alloc] init];
        output.alwaysDiscardsLateVideoFrames = YES;
        output.videoSettings = @{
            (id)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)
        };

        lucent::Cornea cornea;
        std::mutex lock;
        std::deque<lucent::SightFrame> recent_locked;
        std::atomic<uint64_t> last_frame_ms{now_ms()};

        LucentDelegate* delegate = [[LucentDelegate alloc] init];
        delegate.cornea = &cornea;
        delegate.lock = &lock;
        delegate.recentLocked = &recent_locked;
        delegate.lastFrameMs = &last_frame_ms;
        delegate.postTarget = post_target;
        delegate.verboseFrames = verbose;
        delegate.framesDir = nsFramesDir;
        delegate.ciContext = [CIContext contextWithOptions:nil];

        dispatch_queue_t q = dispatch_queue_create("ai.christman.lucent.video", DISPATCH_QUEUE_SERIAL);
        [output setSampleBufferDelegate:delegate queue:q];

        if (![session canAddOutput:output]) {
            std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"camera_error\":\"SESSION_OUTPUT_FAILED\"}\n");
            return 1;
        }
        [session addOutput:output];
        [session commitConfiguration];

        [session startRunning];
        std::fprintf(stderr,
            "{\"organ\":\"LUCENT\",\"protocol\":\"TCAP-V-0044\",\"status\":\"OPEN\","
            "\"device\":\"%s\",\"execution_locality\":\"on_device\",\"left_machine\":false,"
            "\"seat_status\":\"COLD\"}\n",
            device.localizedName.UTF8String);
        std::fflush(stderr);

        bool stdin_open = true;
        bool stall_reported = false;

        while (g_running.load()) {
            if (stdin_open) {
                fd_set rfds;
                FD_ZERO(&rfds);
                FD_SET(STDIN_FILENO, &rfds);
                struct timeval tv{0, 200000};
                int ready = ::select(STDIN_FILENO + 1, &rfds, nullptr, nullptr, &tv);
                if (ready > 0 && FD_ISSET(STDIN_FILENO, &rfds)) {
                    char ch = 0;
                    ssize_t n = ::read(STDIN_FILENO, &ch, 1);
                    if (n == 0) {
                        stdin_open = false;
                    } else if (n > 0) {
                        if (ch == 's' || ch == 'S') g_seat_requested.store(true);
                        if (ch == 'v' || ch == 'V') g_look_requested.store(true);
                        if (ch == 'q' || ch == 'Q') g_running.store(false);
                    }
                }
            } else {
                ::usleep(200000);
            }

            uint64_t age = now_ms() - last_frame_ms.load();
            if (age > 1500 && !stall_reported) {
                std::fprintf(stderr,
                    "{\"organ\":\"LUCENT\",\"sensor_state\":\"NO_SIGNAL\","
                    "\"detail\":\"no camera frames for %llums\"}\n",
                    static_cast<unsigned long long>(age));
                std::fflush(stderr);
                stall_reported = true;
            } else if (age <= 1500) {
                stall_reported = false;
            }
        }

        [session stopRunning];
        std::fprintf(stderr, "{\"organ\":\"LUCENT\",\"status\":\"CLOSED\"}\n");
        std::fflush(stderr);
    }

    return 0;
}
