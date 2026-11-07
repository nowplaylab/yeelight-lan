# yeelight-lan

**English** | [Русский](README.ru.md)

Control Yeelight lamps and light strips over your home network from Python: discovery, colour, brightness and **music mode** for flashing to the beat. No cloud, no account. Comes with a **strip emulator**, so you can build and test without the hardware.

Made by [nowplaylab](https://github.com/nowplaylab) for [NowPlaying](https://github.com/nowplaylab/nowplaying-display), where a strip follows the album cover and pulses with the bass.

## Install

```
pip install git+https://github.com/nowplaylab/yeelight-lan@v0.2.0
```

Python 3.9+. Turn on **LAN Control** for the device in the Yeelight app first.

## Use

```python
import asyncio
import yeelight_lan

devices = yeelight_lan.discover()            # [Device(id, ip, name, model, ...)]
bulb = yeelight_lan.Bulb(devices[0].ip)

async def main():
    await bulb.set_rgb(0xff6600)             # smooth change, 500 ms
    await bulb.set_bright(70)
    print(await bulb.get_props('power', 'bright', 'rgb'))

    # music mode: the device connects to you and takes commands with no reply and no rate limit
    async with bulb.music() as m:
        for level in (20, 100, 40, 100):
            m.send('set_bright', [level, 'sudden', 30])
            await asyncio.sleep(0.25)

asyncio.run(main())
```

Plain commands are limited by the device to **60 per minute per connection**; `Bulb` opens a short connection per command, which also survives Wi-Fi drops and device restarts. For anything faster, use `MusicMode`.

## Emulator

```
python -m yeelight_lan.emulator
```

Pretends to be a Yeelight strip with LAN control on: answers discovery, takes commands on port 55443, enforces the 60/min limit and supports music mode. Open `http://<computer address>:47850` on any device at home to see the strip glow with whatever it was sent, plus a log of commands.

From tests:

```python
strip = await yeelight_lan.emulator.start(port=0, web=None, ssdp=False)
bulb = yeelight_lan.Bulb('127.0.0.1', strip.port)
```

## Licence

MIT.
