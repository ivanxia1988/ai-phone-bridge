/*
 * I2S output to PCM5102 DAC — plays HFP downlink audio on left channel.
 */

#ifndef I2S_OUTPUT_H
#define I2S_OUTPUT_H

#include <stdint.h>
#include <stddef.h>

/* When enabled, I2S output starts at boot with a fixed sample rate
 * instead of waiting for Bluetooth audio connection. */
#define I2S_OUTPUT_ALWAYS_ON        1
#define I2S_OUTPUT_DEFAULT_RATE     16000

/* Test source switch:
 * 0 = normal path (play received PCM via i2s_output_write)
 * 1 = ignore received PCM for DAC and output built-in test tone */
#define I2S_OUTPUT_USE_TEST_TONE    0
#define I2S_OUTPUT_TEST_TONE_HZ     1000
#define I2S_OUTPUT_TEST_TONE_AMP    12000

/**
 * Initialise I2S TX channel for PCM5102 output.
 * Idempotent: first call creates the driver + task; subsequent calls only
 * reconfigure the sample rate if it changed.
 *
 * @param sample_rate  8000 (CVSD) or 16000 (mSBC)
 */
void i2s_output_init(int sample_rate);

/**
 * Tear down I2S output: stop task, free driver and ring buffer.
 * Safe to call even if not initialised.
 */
void i2s_output_deinit(void);

/**
 * Enqueue mono PCM16LE data for I2S playback (left channel).
 * Non-blocking — drops data if the ring buffer is full.
 */
void i2s_output_write(const uint8_t *data, size_t len);

/**
 * Direct blocking write to I2S DMA (bypasses ring buffer).
 * Input format is mono PCM16LE.
 */
void i2s_output_write_direct(const uint8_t *data, size_t len);

/**
 * Retrieve diagnostic counters (NULL-safe for any pointer).
 */
void i2s_output_get_stats(uint32_t *rb_in, uint32_t *rb_drop, uint32_t *dma_out);

#endif /* I2S_OUTPUT_H */
