/* No external audio hardware is connected in the USB-only configuration. */
#include "i2s_output.h"
#include "audio_mixer.h"
void i2s_output_init(int rate) { (void)rate; }
void i2s_output_deinit(void) {}
void i2s_output_write(const uint8_t *data, size_t len) { (void)data; (void)len; }
void i2s_output_write_direct(const uint8_t *data, size_t len) { (void)data; (void)len; }
void i2s_output_get_stats(uint32_t *in, uint32_t *drop, uint32_t *out) {
    if (in) *in = 0;
    if (drop) *drop = 0;
    if (out) *out = 0;
}
void audio_mixer_init(int rate) { (void)rate; }
void audio_mixer_set_enabled(bool enabled) { (void)enabled; }
bool audio_mixer_is_enabled(void) { return false; }
void audio_mixer_push_uart(const uint8_t *data, size_t len) { (void)data; (void)len; }
void audio_mixer_push_bt(const uint8_t *data, size_t len) { (void)data; (void)len; }
