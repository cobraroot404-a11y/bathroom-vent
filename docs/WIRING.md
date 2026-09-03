# Wiring

## Bill of materials

| Qty | Part | Notes |
|---|---|---|
| 1 | ESP32 DevKit V1 (30-pin, WROOM-32) | 3.3 V logic, built-in Wi-Fi |
| 1 | DHT22 / AM2302 humidity+temperature sensor | 0-100 %RH, ±2 %, 0.5 Hz max sample rate |
| 1 | 10 kΩ resistor | DATA pull-up to 3V3 -- omit only if your DHT22 breakout already has one |
| 1 | 1-channel 5 V opto-isolated relay module | ACTIVE-LOW: GPIO LOW = coil energized = fan ON |
| 1 | 5 V / ≥1 A USB power supply + cable | Powers the ESP32 5V pin *and* the relay module's VCC. Relay coil draws ~70 mA -- never power it from the ESP32's 3V3 rail |
| 1 | Exhaust fan (or a multimeter / test lamp standing in for one) | Switched via the relay's NO/COM contacts |
| — | Breadboard + jumper wires | |

## Pin map

| Signal | GPIO | Rationale |
|---|---|---|
| DHT22 DATA | 4 | Not a strapping pin |
| Relay IN | 26 | RTC-capable, so a future deep-sleep build can latch its state through sleep |
| Status LED | 2 | Onboard LED, mirrors fan state |

Do **not** put the relay on GPIO 0, 2, 5, 12 or 15 -- they are strapping pins,
and a coil transient at boot can leave the board stuck in download mode.
Avoid GPIO 6-11 (internal SPI flash) and 34-39 (input-only, can't drive a
relay or read a resistor pull-up the DHT22 protocol needs).

## ASCII diagram

```
                         5V USB supply
                              |
                +-------------+--------------+
                |                             |
                v                             v
        +---------------+           +------------------+
        |  ESP32 DevKit |           |  Relay module     |
        |     V1        |           |  (opto-isolated,  |
        |               |           |   ACTIVE-LOW)     |
        |           5V  |---------->| VCC               |
        |          GND  |---+------>| GND               |
        |               |   |       |                   |
        |      GPIO26   |---)------>| IN  (LOW = fan ON) |
        |               |   |       |                   |
        |       3V3     |---)---+   |     COM  o---------+----> to fan (line)
        |               |   |   |   |     NO   o---------)---> switched hot
        |               |   |   |   +------------------+
        |               |   |   |
        |       GPIO4   |---)---)-------+
        |               |   |   |       |
        |          GND  |---+   |       |
        +---------------+       |       |
                                 |   +---+----+
                                 |   |        |
                                 |  DATA    10k pull-up
                                 |   |        |
                                 |   |        +---- to 3V3 (shared with above)
                                 |   |
                                 |  +--------------+
                                 +--| DHT22 / AM2302 |
                                    |  VCC   GND     |
                                    +--------+-------+
                                             |
                                            GND (shared with ESP32 GND)
```

Key points the diagram is trying to make explicit:

- The DHT22's `DATA` pin needs the 10 kΩ resistor pulled up to **3V3**, not
  5V -- the ESP32's GPIOs are not 5V-tolerant.
- The relay module's `VCC` is powered from the ESP32's **5V** pin (fed by
  the USB supply), never from 3V3 -- the coil needs more current than the
  3V3 regulator on most DevKit boards can safely supply.
- `IN` on the relay is active-low: driving GPIO26 **LOW** energizes the coil
  and turns the fan ON; **HIGH** (the boot-safe idle level) turns it OFF.
- The fan itself connects to the relay's `COM`/`NO` dry contacts, isolated
  from the ESP32 by the module's opto-coupler. If you don't have a fan handy
  for bring-up, a multimeter across `COM`/`NO` (continuity mode) or a
  battery + test lamp works fine to confirm switching.
