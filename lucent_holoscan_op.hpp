// lucent_holoscan_op.hpp — NVIDIA Holoscan Operator for LUCENT (Protocol TCAP-V-0044)

#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <sstream>
#include <string>

#include <holoscan/holoscan.hpp>
#include "lucent_dsp.hpp"

namespace lucent {

class LucentCorneaOp : public holoscan::Operator {
public:
    HOLOSCAN_OPERATOR_FORWARD_ARGS(LucentCorneaOp)
    LucentCorneaOp() = default;

    void setup(holoscan::OperatorSpec& spec) override {
        spec.input<holoscan::gxf::Entity>("frame_in");
        spec.output<std::string>("sight_event_json");
        spec.output<holoscan::gxf::Entity>("verified_frame_out");
        spec.param(tensor_name_, "tensor_name", "Input Tensor Name",
                   "Name of host-accessible uint8 HxWxC (or HxW) tensor", std::string(""));
    }

    void compute(holoscan::InputContext& op_input,
                 holoscan::OutputContext& op_output,
                 holoscan::ExecutionContext& /*context*/) override {
        auto maybe_entity = op_input.receive<holoscan::gxf::Entity>("frame_in");
        const uint64_t ts_ms = now_ms();

        if (!maybe_entity) {
            emit_fault(op_output, SensorState::NO_SIGNAL, ts_ms, "empty entity received");
            return;
        }

        auto entity = maybe_entity.value();
        auto tensor = tensor_name_.get().empty()
                          ? entity.get<holoscan::Tensor>()
                          : entity.get<holoscan::Tensor>(tensor_name_.get().c_str());

        if (!tensor || !tensor->data()) {
            emit_fault(op_output, SensorState::NO_SIGNAL, ts_ms, "null tensor payload");
            return;
        }

        if (tensor->device().device_type != kDLCPU && tensor->device().device_type != kDLCUDAHost) {
            emit_fault(op_output, SensorState::NO_SIGNAL, ts_ms,
                       "tensor is device-only; place FormatConverter(host) upstream of LucentCorneaOp");
            return;
        }

        const auto& shape = tensor->shape();
        if (shape.size() < 2 || shape.size() > 3) {
            emit_fault(op_output, SensorState::NO_SIGNAL, ts_ms, "expected HxW or HxWxC tensor");
            return;
        }

        const int height   = static_cast<int>(shape[0]);
        const int width    = static_cast<int>(shape[1]);
        const int channels = (shape.size() == 3) ? static_cast<int>(shape[2]) : 1;

        std::optional<SightEvent> closed;
        const auto* pixels = static_cast<const uint8_t*>(tensor->data());
        SightFrame live = cornea_.ingest(pixels, width, height, channels, ts_ms, closed);

        if (live.state != SensorState::LOCKED) {
            emit_fault(op_output, live.state, ts_ms, "S.4 gate dropped frame before neural inference");
            return;
        }

        op_output.emit(entity, "verified_frame_out");
        if (closed.has_value()) {
            op_output.emit(closed->to_json(), "sight_event_json");
        }
    }

private:
    static uint64_t now_ms() {
        using namespace std::chrono;
        return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
    }

    static void emit_fault(holoscan::OutputContext& op_output,
                           SensorState state, uint64_t ts_ms, const char* detail) {
        std::ostringstream o;
        o << "{\"organ\":\"LUCENT\",\"protocol\":\"TCAP-V-0044\","
          << "\"execution_locality\":\"on_device\",\"left_machine\":false,"
          << "\"sensor_state\":\"" << to_string(state) << "\","
          << "\"t_ms\":" << ts_ms << ","
          << "\"output_dropped\":true,"
          << "\"detail\":\"" << detail << "\"}";
        op_output.emit(o.str(), "sight_event_json");
    }

    holoscan::Parameter<std::string> tensor_name_;
    Cornea cornea_;
};

} // namespace lucent
