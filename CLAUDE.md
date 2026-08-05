# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repo contains two things:
1. **`ref-gdep133c02/`** — the original manufacturer reference firmware for the GDEP133C02 e-paper display (read-only reference).
2. **`firmware/`** + **`server/`** — the actual application: a WiFi-connected ESP32-S3 photo frame that polls a local Python server for images and displays them, with deep-sleep between updates.

**Display**: GDEP133C02 — 13.3-inch, 1200×1600, 6-color ACeP (Advanced Color ePaper).  
**MCU**: ESP32-S3-WROOM-1-N16R8 (16 MB flash, 8 MB PSRAM).  
**Framework**: ESP-IDF v6.0.1 (CMake).

## Build & Flash (Firmware)

Requires ESP-IDF installed and sourced (`source $IDF_PATH/export.sh`).

```bash
cd firmware

idf.py build                         # Compile
idf.py flash                         # Flash to device
idf.py monitor                       # Open serial monitor
idf.py flash monitor                 # Flash then monitor
idf.py -p /dev/ttyUSB0 flash monitor # Specify port
idf.py menuconfig                    # Interactive Kconfig
idf.py fullclean                     # Remove build directory
```

Target: **ESP32-S3**. Config overrides live in `firmware/sdkconfig.defaults`; run `idf.py menuconfig` to generate the full `sdkconfig`.

## Run (Server)

```bash
cd server
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Web UI: `http://localhost:8000/`

Test dithering:
```bash
python dither.py encode photo.png   # → photo.epd (960000 bytes)
python dither.py decode photo.epd   # → photo_preview.png
```

Target: **ESP32-S3**. Config overrides live in `firmware/sdkconfig.defaults`.

## Architecture

### Firmware layer model

```
app_main.c  →  nvs_config.c (WiFi creds / server URL / poll interval)
            →  captive_portal.c (SoftAP + DNS + HTTP form, first-boot only)
            →  wifi_connect (inline in app_main)
            →  epd_http.c (HTTP GET image → PSRAM → EPD)
                    └── GDEP133C02.c (EPD commands)
                            └── comm.c (SPI/GPIO HAL)
```

**Boot flow**: NVS init → EPD hardware init → load config → if no config: captive portal (blocks until configured, then `esp_restart()`) → WiFi connect → fetch image → display → `esp_deep_sleep(poll_seconds)`.

**Provisioning**: On first boot the device creates a SoftAP `"ESP-Frame-XXXXXX"`. Connect to it and open `http://192.168.4.1/` to configure WiFi credentials, server URL, and poll interval. To re-provision, erase NVS: `idf.py erase-flash` then reflash.

### Firmware key files

| File | Role |
|---|---|
| `main/app_main.c` | Boot sequence, WiFi connect, deep-sleep loop |
| `main/nvs_config.c/h` | NVS read/write for `frame_cfg` namespace |
| `main/captive_portal.c/h` | SoftAP + DNS redirect + config web form |
| `main/epd_http.c/h` | HTTP GET `/frame/{mac}/image` → PSRAM buffer → `pic_display_test()` |
| `main/power_log.c/h` | Deep-sleep cycle accounting in RTC memory (awake/asleep time, wake causes, phase timings) |
| `main/GDEP133C02.c/h` | EPD command layer (init, full refresh, partial update) — from ref |
| `main/comm.c/h` | SPI3_HOST (10 MHz) + GPIO HAL — from ref |
| `main/pindefine.h` | All pin assignments; modify here when retargeting hardware |
| `main/status.h` | `DONE`/`ERROR` codes and `SHOW_LOG` flag |

### Server architecture

```
main.py (FastAPI)
    GET /frame/{mac}/image  →  Plugin.render() → dither.py → 960 KB response
    GET /                   →  templates/index.html  (frame management UI)
    POST /api/frame/{mac}   →  update name/plugin/poll_seconds in config.json
    POST /api/upload        →  save image to uploads/
    GET /api/frame/{mac}/power → power/SoH history + duty-cycle summary

plugins/
    base.py        abstract Plugin.render() → PIL.Image (1200×1600 RGB)
    static.py      single uploaded image
    playlist.py    cycles through a list of plugin configs
    weather.py     Open-Meteo forecast card (no API key)
    earthquakes.py USGS GeoJSON epicenter map

dither.py          PIL image → 960,000-byte EPD binary
config.json        frame registry (auto-created; MAC → name/plugin/last_seen)
soh_history.json   rolling power/SoH samples per MAC (auto-created, capped at 2000)
uploads/           user-uploaded images
```

**Adding a plugin**: Subclass `Plugin` in `plugins/`, set `plugin_type`, implement `render()`. Register the new class in `Plugin.from_config()` in `plugins/base.py` and add it to the `/api/plugins` endpoint in `main.py`.

### Display geometry and data format

- Total resolution: **1200 × 1600 pixels**
- The panel has **two driver ICs**, each responsible for a 600 × 1600 half:
  - CS0 (`SPI_CS0`) = left half (but data is sent right-to-left, so byte order is reversed)
  - CS1 (`SPI_CS1`) = right half
- Pixel encoding: **4 bits per pixel**, two pixels packed per byte (high nibble = first pixel, low nibble = second)
- Color nibble values: `BLACK=0x0`, `WHITE=0x1`, `YELLOW=0x2`, `RED=0x3`, `BLUE=0x5`, `GREEN=0x6`
- Bytes per driver IC: 600/2 × 1600 = **480,000 bytes**; total image = **960,000 bytes**
- Scan direction when filling the buffer: **right-to-left**, top-to-bottom

### SPI chunking

`comm.c` caps single DMA transfers at `SPI_MAX_BUFFER_SIZE = 32768` bytes. `spiTransmitLargeData` handles multi-chunk image writes: the first chunk carries the 8-bit command byte (`DTM = 0x10`), subsequent chunks use `SPI_TRANS_VARIABLE_CMD` with `command_bits = 0`.

### Partial window update constraints

`partialWindowUpdateWithImageData` / `partialWindowUpdateWithoutImageData` enforce hardware register alignment rules:
- `HRST` must be a multiple of 8
- `HRED` must satisfy `(HRED − 7) % 8 == 0`
- `xStart ≤ 584`, `xPixel ≤ 600`
- `yStart + yLine` must be even
- `yStart ≤ 1596`, `yLine ≤ 1600`
- `csx` must be 0 or 1

### Logging

All `printf` debug output is gated on `#if SHOW_LOG` (defined in `status.h`, currently `1`). Set to `0` to silence verbose output.

## Pin Assignments (`pindefine.h`)

| Signal | GPIO |
|---|---|
| SPI_CS0 | 18 |
| SPI_CS1 | 17 |
| SPI_CLK | 9 |
| SPI_Data0–3 | 41, 40, 39, 38 |
| EPD_BUSY (input) | 7 |
| EPD_RST (output) | 6 |
| LOAD_SW (output) | 45 |

Modify only `pindefine.h` when changing hardware wiring.
