"""Yeelight по домашней сети: поиск, команды и музыкальный режим (без облака и аккаунта).

На ленте или лампе в приложении Yeelight должно быть включено «Управление по локальной сети»
(LAN control). Протокол — Yeelight WiFi Light Inter-Operation Specification:
поиск SSDP на 239.255.255.250:1982, команды — строки JSON по TCP 55443.

    import asyncio, yeelight_lan
    devices = yeelight_lan.discover()
    bulb = yeelight_lan.Bulb(devices[0].ip)
    asyncio.run(bulb.set_rgb(0xff6600))

Обычные команды устройство принимает не чаще 60 в минуту на соединение. Для быстрых изменений
(мигание в такт) есть музыкальный режим — MusicMode: устройство само подключается к программе
и принимает команды без ответа и без ограничения частоты.

Эмулятор для проверки без железа: python -m yeelight_lan.emulator
"""
import asyncio
import json
import socket
import time
from dataclasses import dataclass, field

__version__ = '0.1.1'
__all__ = ['Device', 'discover', 'Bulb', 'MusicMode', 'YeelightError']

SSDP = ('239.255.255.250', 1982)
SEARCH = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\nMAN: "ssdp:discover"\r\n'
          'ST: wifi_bulb\r\n\r\n').encode()


class YeelightError(Exception):
    """Устройство не ответило или вернуло ошибку (например, «client quota exceeded»)."""


@dataclass
class Device:
    id: str
    ip: str
    port: int = 55443
    name: str = ''
    model: str = ''
    support: list = field(default_factory=list)
    props: dict = field(default_factory=dict)   # power, bright, rgb, ct, … из ответа на поиск

    @property
    def color(self):
        """Умеет ли цвет (а не только оттенки белого)."""
        return 'set_rgb' in self.support or 'set_hsv' in self.support

    @property
    def music(self):
        """Умеет ли музыкальный режим."""
        return 'set_music' in self.support


def _parse(data):
    h = {}
    for line in data.decode(errors='replace').split('\r\n')[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            h[k.strip().lower()] = v.strip()
    loc = h.get('location', '')
    if not loc.startswith('yeelight://') or not h.get('id'):
        return None
    host, _, port = loc[len('yeelight://'):].partition(':')
    props = {k: h[k] for k in ('power', 'bright', 'color_mode', 'ct', 'rgb', 'hue', 'sat') if k in h}
    return Device(id=h['id'], ip=host, port=int(port or 55443), name=h.get('name', ''), model=h.get('model', ''),
                  support=h.get('support', '').split(), props=props)


def discover(timeout=2.0, target=SSDP):
    """Найти устройства Yeelight в сети (блокирующий вызов, ~timeout секунд)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.3)
    found = {}
    try:
        end = time.time() + timeout
        s.sendto(SEARCH, target)
        again = False
        while time.time() < end:
            if not again and time.time() > end - timeout / 2:
                s.sendto(SEARCH, target)   # UDP может потеряться — спросим ещё раз
                again = True
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            d = _parse(data)
            if d:
                found[d.id] = d
    finally:
        s.close()
    return list(found.values())


class Bulb:
    """Устройство Yeelight по адресу. Каждая команда — своё короткое соединение: так проще
    переживать обрывы Wi-Fi и перезапуски устройства."""

    def __init__(self, ip, port=55443, timeout=3.0):
        self.ip, self.port, self.timeout = ip, port, timeout
        self._id = 0

    async def command(self, method, params=()):
        """Отправить команду, вернуть result (список). YeelightError при ошибке или без ответа."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.ip, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise YeelightError(f'нет связи с {self.ip}: {e}') from None
        try:
            self._id += 1
            cid = self._id
            writer.write(json.dumps({'id': cid, 'method': method, 'params': list(params)}).encode() + b'\r\n')
            await writer.drain()
            end = time.time() + self.timeout
            while time.time() < end:
                line = await asyncio.wait_for(reader.readline(), self.timeout)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get('id') == cid:
                    if 'error' in msg:
                        raise YeelightError(str(msg['error'].get('message')))
                    return msg.get('result')
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            raise YeelightError(f'{method}: {e}') from None
        finally:
            writer.close()
        raise YeelightError(f'{method}: нет ответа')

    # удобные обёртки; effect — 'smooth' или 'sudden', duration — мс
    async def get_props(self, *names):
        names = names or ('power', 'bright', 'color_mode', 'rgb', 'ct', 'hue', 'sat', 'name')
        return dict(zip(names, await self.command('get_prop', names)))

    async def set_power(self, on, effect='smooth', duration=500):
        return await self.command('set_power', ['on' if on else 'off', effect, duration])

    async def set_rgb(self, rgb, effect='smooth', duration=500):
        return await self.command('set_rgb', [int(rgb) & 0xffffff, effect, duration])

    async def set_hsv(self, hue, sat, effect='smooth', duration=500):
        return await self.command('set_hsv', [int(hue) % 360, max(0, min(100, int(sat))), effect, duration])

    async def set_ct(self, kelvin, effect='smooth', duration=500):
        return await self.command('set_ct_abx', [int(kelvin), effect, duration])

    async def set_bright(self, percent, effect='smooth', duration=500):
        return await self.command('set_bright', [max(1, min(100, int(percent))), effect, duration])

    def music(self, local_ip=None):
        return MusicMode(self, local_ip)


class MusicMode:
    """Музыкальный режим: устройство подключается к нам и принимает команды без ответа и лимита.

        async with bulb.music() as m:
            m.send('set_bright', [80, 'sudden', 30])
    """

    def __init__(self, bulb, local_ip=None):
        self.bulb = bulb
        self.local_ip = local_ip
        self._server = None
        self._conn = None

    @property
    def alive(self):
        if not self._conn:
            return False
        r, w = self._conn
        return not (w.is_closing() or r.at_eof())

    async def start(self, timeout=5.0):
        got = asyncio.get_running_loop().create_future()

        def on_conn(r, w):
            if got.done():
                w.close()
            else:
                got.set_result((r, w))
        self._server = await asyncio.start_server(on_conn, '0.0.0.0', 0)
        port = self._server.sockets[0].getsockname()[1]
        ip = self.local_ip
        if not ip:   # свой адрес, через который видно устройство
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect((self.bulb.ip, 1))
                ip = probe.getsockname()[0]
            finally:
                probe.close()
        try:
            await self.bulb.command('set_music', [1, ip, port])
            self._conn = await asyncio.wait_for(got, timeout)
        except (YeelightError, asyncio.TimeoutError) as e:
            await self.stop()
            raise YeelightError(f'музыкальный режим не включился: {e}') from None
        return self

    def send(self, method, params=()):
        """Отправить команду (без ответа). False — соединение оборвалось."""
        if not self.alive:
            return False
        try:
            self._conn[1].write(json.dumps({'id': 1, 'method': method, 'params': list(params)}).encode() + b'\r\n')
            return True
        except (OSError, RuntimeError):
            return False

    async def stop(self):
        if self._conn:
            self._conn[1].close()
            self._conn = None
        if self._server:
            self._server.close()
            self._server = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()
