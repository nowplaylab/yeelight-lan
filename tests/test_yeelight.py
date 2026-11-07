"""Проверки на встроенном эмуляторе: команды, лимит частоты, музыкальный режим, поиск."""
import asyncio
import time
import unittest

import yeelight_lan
from yeelight_lan import emulator


class YeelightTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.strip = await emulator.start(port=0, web=None, ssdp=False, host='127.0.0.1')
        self.bulb = yeelight_lan.Bulb('127.0.0.1', self.strip.port)

    async def asyncTearDown(self):
        self.strip.server.close()

    async def test_commands(self):
        await self.bulb.set_rgb(0x123456)
        await self.bulb.set_bright(42)
        props = await self.bulb.get_props('rgb', 'bright', 'color_mode', 'power')
        self.assertEqual(props, {'rgb': str(0x123456), 'bright': '42', 'color_mode': '1', 'power': 'on'})
        await self.bulb.set_ct(2700)
        self.assertEqual((await self.bulb.get_props('color_mode', 'ct')), {'color_mode': '2', 'ct': '2700'})
        await self.bulb.set_power(False)
        self.assertEqual(self.strip.power, 'off')

    async def test_unknown_method(self):
        with self.assertRaises(yeelight_lan.YeelightError):
            await self.bulb.command('no_such_method', [])

    async def test_no_device(self):
        with self.assertRaises(yeelight_lan.YeelightError):
            await yeelight_lan.Bulb('127.0.0.1', 1, timeout=1).set_rgb(1)

    async def test_quota(self):
        # лимит — 60 команд в минуту на одно соединение: шлём 61 по одному соединению
        reader, writer = await asyncio.open_connection('127.0.0.1', self.strip.port)
        for i in range(61):
            writer.write(b'{"id":%d,"method":"set_bright","params":[50,"sudden",0]}\r\n' % (i + 1))
        await writer.drain()
        errors = 0
        for _ in range(61 * 2):
            line = await asyncio.wait_for(reader.readline(), 3)
            errors += b'quota' in line
            if errors:
                break
        writer.close()
        self.assertEqual(errors, 1)

    async def test_music_mode(self):
        async with self.bulb.music(local_ip='127.0.0.1') as m:
            self.assertTrue(m.alive)
            for b in (10, 90, 30):
                self.assertTrue(m.send('set_bright', [b, 'sudden', 0]))
            await asyncio.sleep(0.3)
        self.assertEqual(self.strip.bright, 30)
        self.assertEqual(self.strip.music_cmds, 3)

    async def test_music_mode_is_immediate(self):
        # мелкие команды подряд не должны копиться в пачки: каждая доходит почти сразу
        async with self.bulb.music(local_ip='127.0.0.1') as m:
            await asyncio.sleep(0.1)
            delays = []
            for i in range(20):
                n = len(self.strip.music_times)
                sent = time.perf_counter()
                m.send('set_bright', [10 + i, 'sudden', 0])
                while len(self.strip.music_times) == n and time.perf_counter() - sent < 1:
                    await asyncio.sleep(0.001)
                delays.append(self.strip.music_times[-1] - sent)
                await asyncio.sleep(0.01)
        self.assertLess(max(delays), 0.05, delays)

    async def test_discover(self):
        strip = await emulator.start(name='Поиск', port=0, web=None, ssdp=True)
        await asyncio.sleep(0.2)
        try:
            found = await asyncio.get_running_loop().run_in_executor(
                None, lambda: yeelight_lan.discover(1.0, target=('127.0.0.1', 1982)))
        finally:
            strip.server.close()
            if strip.ssdp:
                strip.ssdp.close()
        mine = [d for d in found if d.id == strip.id]
        if not mine:
            self.skipTest('порт поиска 1982 занят (например, уже запущен эмулятор)')
        d = mine[0]
        self.assertEqual((d.name, d.port, d.color, d.music), ('Поиск', strip.port, True, True))


if __name__ == '__main__':
    unittest.main()
