/*
 * Simple two-source mono PCM16 mixer for I2S output.
 *
 * Design goal: both sources must be active. If one side stops,
 * output is forced to silence instead of letting the other continue.
 */

#ifndef AUDIO_MIXER_H
#define AUDIO_MIXER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

void audio_mixer_init(int sample_rate);
void audio_mixer_set_enabled(bool enabled);
bool audio_mixer_is_enabled(void);

void audio_mixer_push_uart(const uint8_t *data, size_t len);
void audio_mixer_push_bt(const uint8_t *data, size_t len);

#endif /* AUDIO_MIXER_H */
