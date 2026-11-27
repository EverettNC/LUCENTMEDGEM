CXX := clang++
CXXFLAGS := -std=c++20 -O2 -fobjc-arc
FRAMEWORKS := -framework AVFoundation -framework CoreMedia -framework CoreVideo \
              -framework CoreGraphics -framework CoreImage -framework ImageIO \
              -framework Foundation

all: lucent_live

lucent_live: lucent_live.mm lucent_dsp.hpp
	$(CXX) $(CXXFLAGS) lucent_live.mm $(FRAMEWORKS) -o lucent_live

clean:
	rm -f lucent_live

.PHONY: all clean
