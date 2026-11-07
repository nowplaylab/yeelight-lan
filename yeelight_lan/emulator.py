"""Эмулятор ленты Yeelight (Lightstrip) — чтобы делать и проверять управление без настоящей ленты.

Ведёт себя как лента с включённым «Управлением по локальной сети» (LAN control):
- отвечает на поиск SSDP (239.255.255.250:1982, ST: wifi_bulb);
- принимает команды JSON по TCP 55443: set_power, toggle, set_bright, set_rgb, set_hsv,
  set_ct_abx, start_cf/stop_cf, get_prop, set_music, set_name;
- как настоящая лента, ограничивает обычные команды: не больше 60 в минуту на соединение
  (лишние получают ошибку «client quota exceeded»);
- «музыкальный режим» (set_music [1, host, port]): лента сама подключается к host:port и
  принимает оттуда команды без ответа и без ограничения частоты — так делают мигание в такт.

Показ: http://<адрес компьютера>:47850 (с любого устройства дома) — полоса светится текущим цветом и яркостью, внизу журнал команд.

    python -m yeelight_lan.emulator [--name "Лента"] [--port 55443] [--web 47850]

Из своих проверок: strip = await yeelight_lan.emulator.start(port=0, web=None, ssdp=False)
"""
import argparse
import asyncio
import colorsys
import hashlib
import json
import socket
import struct

import yeelight_lan
import time

from aiohttp import web

SSDP_ADDR, SSDP_PORT = '239.255.255.250', 1982
SUPPORT = ('get_prop set_default set_power toggle set_bright start_cf stop_cf set_scene cron_add cron_get '
           'cron_del set_ct_abx set_rgb set_hsv set_adjust adjust_bright adjust_ct adjust_color set_music set_name')


class Strip:
    def __init__(self, name, port):
        # постоянный номер: после перезапуска эмулятор — та же «лента», выбирать заново не надо
        self.id = '0x' + hashlib.sha1(f'{socket.gethostname()}|{name}|{port}'.encode()).hexdigest()[:16]
        self.name = name
        self.port = port
        self.power = 'on'
        self.bright = 80
        self.color_mode = 1           # 1 — rgb, 2 — ct, 3 — hsv
        self.rgb = 0xff8800
        self.ct = 4000
        self.hue, self.sat = 30, 100
        self.flowing = 0
        self.music_on = 0
        self.log = []                 # (время, откуда, команда)
        self.changes = asyncio.Event()
        self.quota = {}               # соединение -> [времена команд за минуту]
        self.music_cmds = 0
        self.music_started = 0
        self.music_times = []         # time.perf_counter() прихода команд музыкального режима (для замеров)

    # ---------- состояние ----------

    def color_hex(self):
        if self.color_mode == 2:   # белый: приблизим цвет по температуре
            k = max(1700, min(6500, self.ct))
            t = (k - 1700) / 4800
            r, g, b = 255, int(170 + 70 * t), int(80 + 175 * t)
            return '#%02x%02x%02x' % (r, g, b)
        if self.color_mode == 3:
            r, g, b = colorsys.hsv_to_rgb(self.hue / 360, self.sat / 100, 1)
            return '#%02x%02x%02x' % (int(r * 255), int(g * 255), int(b * 255))
        return '#%06x' % self.rgb

    def props(self):
        return {'power': self.power, 'bright': str(self.bright), 'ct': str(self.ct), 'rgb': str(self.rgb),
                'hue': str(self.hue), 'sat': str(self.sat), 'color_mode': str(self.color_mode),
                'flowing': str(self.flowing), 'music_on': str(self.music_on), 'name': self.name}

    def note(self, src, text):
        self.log.append((time.strftime('%H:%M:%S'), src, text))
        del self.log[:-60]
        self.changes.set()

    # ---------- команды ----------

    def run(self, method, params):
        """-> result (список) или поднимает ValueError с текстом ошибки."""
        if method == 'get_prop':
            p = self.props()
            return [p.get(k, '') for k in params]
        if method == 'set_power':
            self.power = params[0]
        elif method == 'toggle':
            self.power = 'off' if self.power == 'on' else 'on'
        elif method == 'set_bright':
            self.bright = max(1, min(100, int(params[0])))
        elif method == 'set_rgb':
            self.rgb = int(params[0]) & 0xffffff
            self.color_mode = 1
            self.power = 'on'
        elif method == 'set_hsv':
            self.hue, self.sat = int(params[0]) % 360, max(0, min(100, int(params[1])))
            self.color_mode = 3
            self.power = 'on'
        elif method == 'set_ct_abx':
            self.ct = max(1700, min(6500, int(params[0])))
            self.color_mode = 2
            self.power = 'on'
        elif method == 'start_cf':
            self.flowing = 1
        elif method == 'stop_cf':
            self.flowing = 0
        elif method == 'set_name':
            self.name = str(params[0])
        elif method == 'set_music':
            pass   # обрабатывается в соединении
        else:
            raise ValueError('method not supported')
        return ['ok']


class _Ssdp(asyncio.DatagramProtocol):
    def __init__(self, strip):
        self.strip = strip
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        text = data.decode(errors='replace')
        if 'M-SEARCH' not in text or 'wifi_bulb' not in text:
            return
        # адрес, с которого нас видно этому клиенту
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((addr[0], 1))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
        strip, p = self.strip, self.strip.props()
        reply = ('HTTP/1.1 200 OK\r\nCache-Control: max-age=3600\r\nDate: \r\nExt: \r\n'
                 f'Location: yeelight://{ip}:{strip.port}\r\nServer: POSIX UPnP/1.0 YGLC/1\r\n'
                 f'id: {strip.id}\r\nmodel: strip6\r\nfw_ver: 18\r\nsupport: {SUPPORT}\r\n'
                 f'power: {p["power"]}\r\nbright: {p["bright"]}\r\ncolor_mode: {p["color_mode"]}\r\n'
                 f'ct: {p["ct"]}\r\nrgb: {p["rgb"]}\r\nhue: {p["hue"]}\r\nsat: {p["sat"]}\r\nname: {p["name"]}\r\n\r\n')
        self.transport.sendto(reply.encode(), addr)
        strip.note(addr[0], 'поиск в сети → ответила')


async def ssdp(strip):
    """Отвечаем на поиск ленты в сети (DatagramProtocol — работает на Python 3.9+ и на всех системах)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', SSDP_PORT))
    joined = set()

    def join():
        # слушаем поиск в каждой сети компьютера (а не только в той, что система считает главной —
        # с VPN это туннель); сети появляются и пропадают, поэтому проверяем раз в 30 с
        for ip in yeelight_lan.local_ips() + ['127.0.0.1']:
            if ip in joined:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton(SSDP_ADDR) + socket.inet_aton(ip))
                joined.add(ip)
            except OSError:
                pass
    join()
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Ssdp(strip), sock=sock)

    async def rejoin():
        while not transport.is_closing():
            await asyncio.sleep(30)
            join()
    strip.ssdp_task = asyncio.create_task(rejoin())
    return transport


async def handle(strip, reader, writer):
    peer = writer.get_extra_info('peername')[0]
    times = []
    strip.note(peer, 'подключение')
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                cmd = json.loads(line)
                cid, method, params = cmd.get('id'), cmd['method'], cmd.get('params', [])
            except (ValueError, KeyError):
                writer.write(b'{"id":0,"error":{"code":-1,"message":"invalid command"}}\r\n')
                continue
            now = time.time()
            times[:] = [t for t in times if now - t < 60]
            if len(times) >= 60 and method != 'get_prop':
                writer.write(json.dumps({'id': cid, 'error': {'code': -1, 'message': 'client quota exceeded'}}).encode() + b'\r\n')
                strip.note(peer, f'{method}: отказ — больше 60 команд в минуту')
                continue
            times.append(now)
            if method == 'set_music':
                if params and int(params[0]) == 1:
                    asyncio.create_task(music(strip, params[1], int(params[2])))
                    result = ['ok']
                else:
                    strip.music_on = 0
                    result = ['ok']
            else:
                try:
                    result = strip.run(method, params)
                except (ValueError, IndexError, TypeError) as e:
                    writer.write(json.dumps({'id': cid, 'error': {'code': -1, 'message': str(e)}}).encode() + b'\r\n')
                    continue
            writer.write(json.dumps({'id': cid, 'result': result}).encode() + b'\r\n')
            if method != 'get_prop':
                writer.write(json.dumps({'method': 'props', 'params': strip.props()}).encode() + b'\r\n')
                strip.note(peer, f'{method} {params}')
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        strip.note(peer, 'отключение')
        writer.close()


async def music(strip, host, port):
    """Музыкальный режим: лента подключается к программе и слушает команды без ответов и лимита."""
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as e:
        strip.note(host, f'музыкальный режим: не подключилась ({e})')
        return
    strip.music_on = 1
    strip.music_started = time.time()
    strip.music_cmds = 0
    strip.note(host, f'музыкальный режим: подключилась к {host}:{port}')
    try:
        while strip.music_on:
            line = await reader.readline()
            if not line:
                break
            try:
                cmd = json.loads(line)
                strip.music_times.append(time.perf_counter())
                del strip.music_times[:-500]
                strip.run(cmd['method'], cmd.get('params', []))
                strip.music_cmds += 1
                strip.changes.set()
            except (ValueError, KeyError, IndexError, TypeError):
                pass
    finally:
        strip.music_on = 0
        strip.note(host, f'музыкальный режим: закрыт ({strip.music_cmds} команд)')
        writer.close()


PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Эмулятор ленты Yeelight</title>
<style>
body{margin:0;background:#0d0d0f;color:#eee;font:15px/1.4 system-ui,sans-serif;padding:32px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#888;margin-bottom:28px}
.wall{height:220px;border-radius:14px;background:#141417;position:relative;overflow:hidden}
.glow{position:absolute;inset:0;transition:background .12s, opacity .12s}
.strip{position:absolute;left:6%;right:6%;top:46%;height:14px;border-radius:7px;transition:background .12s, box-shadow .12s, opacity .12s}
.stats{display:flex;gap:28px;margin:18px 0;color:#aaa}.stats b{color:#fff;font-weight:600}
.log{font:13px/1.6 ui-monospace,Consolas,monospace;color:#9a9aa5;max-height:300px;overflow:auto;background:#141417;border-radius:10px;padding:12px 14px}
</style>
<h1 id="name">Лента</h1><div class="sub">Эмулятор Yeelight Lightstrip · LAN 55443 · поиск SSDP</div>
<div class="wall"><div class="glow" id="glow"></div><div class="strip" id="strip"></div></div>
<div class="stats"><span>Питание: <b id="power"></b></span><span>Цвет: <b id="color"></b></span><span>Яркость: <b id="bright"></b></span>
<span>Музыкальный режим: <b id="music"></b></span></div>
<div class="log" id="log"></div>
<script>
const $=id=>document.getElementById(id);
const es=new EventSource('/events');
es.onmessage=e=>{const s=JSON.parse(e.data);
 const on=s.power==='on', a=on?Math.max(.08,s.bright/100):0;
 $('strip').style.background=on?s.hex:'#2a2a2e'; $('strip').style.opacity=on?(.35+.65*s.bright/100):1;
 $('strip').style.boxShadow=on?`0 0 ${20+60*s.bright/100}px ${10+30*s.bright/100}px ${s.hex}`:'none';
 $('glow').style.background=`radial-gradient(ellipse 70% 90% at 50% 50%, ${s.hex}, transparent 70%)`; $('glow').style.opacity=a*.6;
 $('name').textContent=s.name; $('power').textContent=on?'вкл':'выкл'; $('color').textContent=s.hex.toUpperCase();
 $('bright').textContent=s.bright+'%'; $('music').textContent=s.music?('да, '+s.music_rate+' команд/с'):'нет';
 $('log').innerHTML=s.log.slice().reverse().map(l=>l.join('  ')).join('<br>');};
</script>'''


async def web_app(strip, port):
    async def page(r):
        return web.Response(text=PAGE, content_type='text/html')

    async def events(r):
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store'})
        await resp.prepare(r)
        last_n, last_t = 0, time.time()
        try:
            while True:
                rate = (strip.music_cmds - last_n) / max(0.05, time.time() - last_t)
                last_n, last_t = strip.music_cmds, time.time()
                st = {'hex': strip.color_hex(), 'bright': strip.bright, 'power': strip.power, 'name': strip.name,
                      'music': strip.music_on, 'music_rate': round(rate, 1), 'log': strip.log[-25:]}
                await resp.write(f'data: {json.dumps(st, ensure_ascii=False)}\n\n'.encode())
                strip.changes.clear()
                try:
                    await asyncio.wait_for(strip.changes.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(1 / 30)
        except (ConnectionError, asyncio.CancelledError):
            pass
        return resp

    app = web.Application()
    app.add_routes([web.get('/', page), web.get('/events', events),
                    web.get('/state', lambda r: web.json_response(dict(strip.props(), hex=strip.color_hex(),
                                                                       music_cmds=strip.music_cmds)))])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', port).start()   # показ можно открыть с телефона или телевизора


async def start(name='Лента (эмулятор)', port=55443, web=47850, ssdp=True, host='0.0.0.0'):
    """Запустить эмулятор в текущем цикле asyncio. port=0 — любой свободный (смотри strip.port).
    web=None — без страницы показа, ssdp=False — не отвечать на поиск в сети."""
    strip = Strip(name, port)
    server = await asyncio.start_server(lambda r, w: handle(strip, r, w), host, port)
    strip.port = server.sockets[0].getsockname()[1]
    strip.server = server
    if web:
        await web_app(strip, web)
    strip.ssdp = None
    if ssdp:
        try:
            strip.ssdp = await globals()['ssdp'](strip)
        except OSError as e:   # порт 1982 занят — без поиска, по адресу подключиться можно
            strip.note('-', f'поиск в сети недоступен: {e}')
    return strip


async def main():
    ap = argparse.ArgumentParser(description='Эмулятор ленты Yeelight')
    ap.add_argument('--name', default='Лента (эмулятор)')
    ap.add_argument('--port', type=int, default=55443)
    ap.add_argument('--web', type=int, default=47850)
    a = ap.parse_args()
    strip = await start(a.name, a.port, a.web)
    print(f'Эмулятор ленты: LAN {strip.port}, показ http://<адрес компьютера>:{a.web}', flush=True)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
