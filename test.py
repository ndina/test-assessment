import asyncio
from typing import Optional

import asyncpg
import aiohttp
import argparse
import ipaddress
from aiohttp import web
from aiohttp.web import Response
from aiohttp.web_request import Request

DB_CONN_INFO = {
    'user': 'docker',
    'password': 'docker',
    'host': 'postgres',
    'database': 'postgres',
    'port': 5432
}


def process_result_to_json(db_out):
    res = []
    for i in db_out:
        json = {
            'id': i['id'],
            'ip': i['ip'],
            'port': i['port'],
            'available': i['available'],
        }
        res.append(json)
    return res


def is_valid_ipv4(ip_string: str) -> bool:
    try:
        ipaddress.IPv4Address(ip_string)
        return True
    except ValueError:
        return False


def is_port_valid(port: int) -> bool:
    return 1024 <= port <= 65535


async def is_port_open(ip_address: str, port: int, timeout: float) -> bool:
    try:
        connection = asyncio.open_connection(ip_address, port)
        await asyncio.wait_for(connection, timeout=timeout)
        return True
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
        return False


class Handler:

    async def get_record(self, request: Request) -> Response:
        ip = request.query.get('ip')
        if not ip:
            return web.json_response({'message': 'ip is required'}, status=400)

        if not is_valid_ipv4(ip):
            return web.json_response({'message': 'invalid ip'}, status=400)

        port = request.query.get('port')
        if port is not None:
            try:
                port = int(port)
                port_valid = is_port_valid(port)
            except ValueError:
                port_valid = False

            if not port_valid:
                return web.json_response({'message': 'invalid port'}, status=400)

        query = f"SELECT * FROM services WHERE ip = '{ip}'"
        if port:
            query += f' AND port = {port}'

        async with request.app['pool'].acquire() as conn:
            result = await conn.fetch(query)

        return aiohttp.web.json_response(process_result_to_json(result))

    async def post_record(self, request: Request) -> Response:
        data = await request.json()
        ip = data.get('ip')
        port = data.get('port')
        available = data.get('available')
        if ip is None or port is None or available is None:
            return web.json_response({'message': 'ip, port and available are required'}, status=400)

        if not is_valid_ipv4(ip):
            return web.json_response({'message': 'invalid ip'}, status=400)

        if port is not None:
            try:
                port = int(port)
                port_valid = is_port_valid(port)
            except ValueError:
                port_valid = False

            if not port_valid:
                return web.json_response({'message': 'invalid port'}, status=400)

        async with request.app['pool'].acquire() as conn:
            await conn.execute('''INSERT INTO services(ip, port, available) VALUES ($1, $2, $3)''', ip, port, available)
            return web.json_response(data, status=201)

    async def delete_record(self, request: Request) -> Response:
        async with request.app['pool'].acquire() as conn:
            try:
                service_id = int(request.match_info['id'])
            except ValueError:
                return web.json_response({'message': 'invalid id'}, status=400)
            await conn.execute('''DELETE FROM services WHERE id = $1''', service_id)
            return aiohttp.web.json_response({'success': 204}, status=204)


class Database:
    def __init__(self):
        self.pool = None

    @classmethod
    async def create(cls):
        instance = cls()
        instance.pool = await asyncpg.create_pool(**DB_CONN_INFO)
        return instance

    async def create_table(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('''DROP TABLE IF EXISTS services''')
                await conn.execute('''
                               CREATE TABLE services (
                                 id SERIAL PRIMARY KEY,
                                 ip VARCHAR(255) NOT NULL,
                                 port INTEGER NOT NULL,
                                 available BOOLEAN NOT NULL
                               )   
                           ''')

    async def insert_data(self):
        """Init database with data"""
        data = [
            {'available': True, 'ip': '127.0.0.1', 'port': 44444},
            {'available': False, 'ip': '127.0.0.1', 'port': 55555},
            {'available': True, 'ip': '127.0.0.1', 'port': 11111},
            {'available': True, 'ip': '127.0.0.1', 'port': 22222},
            {'available': True, 'ip': '127.0.0.1', 'port': 33333},
        ]
        async with self.pool.acquire() as conn:
            for service in data:
                await conn.execute(
                    '''INSERT INTO services(ip, port, available) VALUES ($1, $2, $3)''',
                    service['ip'], service['port'], service['available']
                )


async def process_service_availability(pool: asyncpg.pool.Pool, id_: int, ip: str, port: int) -> None:
    available = False
    if await is_port_open(ip, port, timeout=1):
        available = True
    print(ip, port, available)
    async with pool.acquire() as conn:
        await conn.execute('UPDATE services SET available = $1 WHERE id = $2;', available, id_)


async def start_background_task(pool: Optional[asyncpg.pool.Pool] = None):
    if not pool:
        db_connection = await Database.create()
        pool = db_connection.pool

    while True:
        coroutines = []
        async with pool.acquire() as conn:
            result = await conn.fetch('SELECT * FROM services;')

        for res in result:
            coroutines.append(process_service_availability(pool, res['id'], res['ip'], res['port']))

        await asyncio.gather(*coroutines)

        await asyncio.sleep(10)


async def on_start_bg_task(app_: web.Application) -> None:
    asyncio.create_task(start_background_task(app_['pool']))


async def init_db(app_: web.Application) -> None:
    db_ = await Database.create()
    app_['pool'] = db_.pool


async def close_db(app_: web.Application) -> None:
    await app_['pool'].close()


def run_app(host: str, port: int, run_bg_task: bool) -> None:
    app = web.Application()
    app.on_startup.append(init_db)
    if run_bg_task:
        app.on_startup.append(on_start_bg_task)
    app.on_cleanup.append(close_db)

    handler = Handler()
    app.add_routes([
        web.get('/records', handler.get_record),
        web.post('/records', handler.post_record),
        web.delete('/records/{id}', handler.delete_record)
    ])

    web.run_app(app, host=host, port=port)


async def run_migrations() -> None:
    db_ = await Database.create()
    await db_.create_table()
    await db_.insert_data()
    await db_.pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', help='IPv4 address API server would listen on', default='0.0.0.0', required=False)
    parser.add_argument('--port', help='TCP port API server would listen on', default=8000, type=int, required=False)
    parser.add_argument('--run-background-task', default=False, type=bool, required=False)
    parser.add_argument('--background-task-only', default=False, type=bool, required=False)
    parser.add_argument('--migrations', default=False, type=bool, required=False)
    args = parser.parse_args()

    if args.migrations:
        asyncio.run(run_migrations())

    elif args.run_background_task and args.background_task_only:
        asyncio.run(start_background_task())
    else:
        run_app(args.host, args.port, args.run_background_task)
