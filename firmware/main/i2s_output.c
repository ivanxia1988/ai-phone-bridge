/*
 * I2S output to PCM5102 DAC - plays HFP downlink audio on left channel.
 *
 * PCM5102 wiring assumptions:
 *   FMT  = GND  -> standard I2S (Philips) format
 *   XSMT = 3.3V -> unmuted
 *   BCK  -> GPIO 26
 *   LRCK -> GPIO 22
 *   DIN  -> GPIO 25
 */

#include "i2s_output.h"

#include <string.h>
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"

#define I2S_OUT_TAG "I2S_OUT"

/* ---- GPIO ---- */
#define I2S_OUT_BCLK_GPIO   26
#define I2S_OUT_WS_GPIO     22
#define I2S_OUT_DOUT_GPIO   25

/* ---- Buffers ---- */
#define I2S_OUT_RINGBUF_SIZE  8192   /* mono PCM bytes before conversion */

static i2s_chan_handle_t s_tx_chan;
static RingbufHandle_t s_i2s_rb;
static TaskHandle_t s_i2s_task;
static bool s_inited;
static int s_current_rate;

/* diagnostic counters */
static uint32_t s_rb_in;       /* bytes enqueued to ring buffer */
static uint32_t s_rb_drop;     /* bytes dropped (ring buffer full) */
static uint32_t s_dma_out;     /* bytes written to I2S DMA */

static void i2s_output_write_mono_blocking(const uint8_t *data, size_t len)
{
    if (!s_tx_chan || !data || len < 2) {
        return;
    }

    size_t mono_bytes = len & ~((size_t)1); /* int16 alignment */
    int16_t stereo_buf[512];                /* 256 stereo frames */

    while (mono_bytes > 0) {
        size_t chunk = mono_bytes > 512 ? 512 : mono_bytes; /* mono bytes */
        const int16_t *mono = (const int16_t *)data;
        size_t mono_samples = chunk / 2;

        for (size_t i = 0; i < mono_samples; i++) {
            stereo_buf[i * 2]     = mono[i];
            stereo_buf[i * 2 + 1] = mono[i];
        }

        size_t bytes_written = 0;
        i2s_channel_write(s_tx_chan, stereo_buf, mono_samples * 4, &bytes_written, portMAX_DELAY);
        s_dma_out += (uint32_t)bytes_written;

        data += chunk;
        mono_bytes -= chunk;
    }
}

/* Writer task: ring buffer consumer -> I2S DMA */
static void i2s_output_task(void *arg)
{
    (void)arg;
    while (true) {
        size_t item_size = 0;
        uint8_t *data = (uint8_t *)xRingbufferReceiveUpTo(s_i2s_rb, &item_size, portMAX_DELAY, 512);
        if (!data || item_size == 0) {
            continue;
        }

        i2s_output_write_mono_blocking(data, item_size);
        vRingbufferReturnItem(s_i2s_rb, data);
    }
}

void i2s_output_init(int sample_rate)
{
    if (s_inited) {
        if (s_current_rate != sample_rate) {
            i2s_channel_disable(s_tx_chan);
            i2s_std_clk_config_t clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(sample_rate);
            i2s_channel_reconfig_std_clock(s_tx_chan, &clk_cfg);
            i2s_channel_enable(s_tx_chan);
            s_current_rate = sample_rate;
            ESP_LOGI(I2S_OUT_TAG, "Sample rate changed to %d", sample_rate);
        }
        return;
    }

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &s_tx_chan, NULL));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(sample_rate),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = I2S_OUT_BCLK_GPIO,
            .ws = I2S_OUT_WS_GPIO,
            .dout = I2S_OUT_DOUT_GPIO,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx_chan, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx_chan));
    s_current_rate = sample_rate;

    s_i2s_rb = xRingbufferCreate(I2S_OUT_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    if (!s_i2s_rb) {
        ESP_LOGE(I2S_OUT_TAG, "ring buffer alloc failed");
        return;
    }

    xTaskCreate(i2s_output_task, "i2sOut", 4096, NULL, configMAX_PRIORITIES - 4, &s_i2s_task);

    s_inited = true;
    ESP_LOGI(I2S_OUT_TAG, "I2S output ready: BCLK=%d WS=%d DOUT=%d rate=%d",
             I2S_OUT_BCLK_GPIO, I2S_OUT_WS_GPIO, I2S_OUT_DOUT_GPIO, sample_rate);
}

void i2s_output_deinit(void)
{
    if (!s_inited) {
        return;
    }
    s_inited = false;

    if (s_i2s_task) {
        vTaskDelete(s_i2s_task);
        s_i2s_task = NULL;
    }

    if (s_tx_chan) {
        i2s_channel_disable(s_tx_chan);
        i2s_del_channel(s_tx_chan);
        s_tx_chan = NULL;
    }

    if (s_i2s_rb) {
        vRingbufferDelete(s_i2s_rb);
        s_i2s_rb = NULL;
    }

    s_current_rate = 0;
    ESP_LOGI(I2S_OUT_TAG, "I2S output deinitialized");
}

void i2s_output_write(const uint8_t *data, size_t len)
{
    if (!s_i2s_rb || !data || len == 0) {
        return;
    }

    if (xRingbufferSend(s_i2s_rb, data, len, 0) == pdTRUE) {
        s_rb_in += (uint32_t)len;
    } else {
        s_rb_drop += (uint32_t)len;
    }
}

void i2s_output_write_direct(const uint8_t *data, size_t len)
{
    i2s_output_write_mono_blocking(data, len);
}

void i2s_output_get_stats(uint32_t *rb_in, uint32_t *rb_drop, uint32_t *dma_out)
{
    if (rb_in) {
        *rb_in = s_rb_in;
    }
    if (rb_drop) {
        *rb_drop = s_rb_drop;
    }
    if (dma_out) {
        *dma_out = s_dma_out;
    }
}
