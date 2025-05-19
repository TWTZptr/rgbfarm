import ipaddress

import asyncpg
from aiohttp import web

import exceptions
import models


async def get_teams(request: web.Request):
    sq = '''select team_id, team_name, team_ip
            from t_teams
            where team_id >= 0;'''

    pool = request.app['pool']

    async with pool.acquire() as conn:
        teams = await conn.fetch(sq)

    return web.json_response(
        {
            'status': 'ok',
            'teams': list(map(lambda t: dict(t), teams))
        }
    )


async def delete_teams(request: web.Request):
    team_id = request.rel_url.query.get('team_id')
    team_id = int(team_id) if team_id else None

    sq = '''delete
            from t_teams
            where team_id >= 0
                and (($1::int is not null and team_id = $1) or $1 is null);'''

    pool = request.app['pool']

    async with pool.acquire() as conn:
        await conn.execute(sq, team_id)

    return web.json_response(
        {
            'status': 'ok'
        }
    )


async def add_team(request: web.Request):
    user_data = await request.json()

    teams = list(map(
        lambda t: models.Team(team_name=t.get('team_name').strip(), team_ip=t.get('team_ip').strip()),
        user_data.get('teams')
    ))

    for team in teams:
        try:
            ipaddress.ip_address(team.team_ip)
        except ValueError:
            raise exceptions.IpAddressInvalid

    pool = request.app['pool']
    created_teams = []

    async with pool.acquire() as conn:
        for team in teams:
            try:
                sq = '''insert into t_teams (team_name, team_ip)
                        values ($1, $2)
                        returning team_id, team_name, team_ip;'''

                created_teams.append(await conn.fetchrow(sq, team.team_name, team.team_ip))
            except asyncpg.exceptions.UniqueViolationError:
                raise exceptions.TeamExists

    return web.json_response(
        {
            'status': 'ok',
            'teams': list(map(lambda t: dict(t), created_teams))
        }
    )


async def generate_teams(request: web.Request):
    user_data = await request.json()

    name_template = user_data.get('name_template').strip()
    ip_template = user_data.get('ip_template').strip()
    start_num = user_data.get('start_num')
    count_teams = int(user_data.get('count_teams'))

    try:
        ip_version = ipaddress.ip_address(ip_template.replace('$', '0')).version
    except ValueError:
        raise exceptions.IpAddressInvalid

    start_num = int(start_num, 10 if ip_version == 4 else 16)
    if start_num < 0 or start_num + count_teams > (256 if ip_version == 4 else 0xffff):
        raise exceptions.IpAddressInvalid

    team_generator = (
        (
            name_template.replace('$', str(team_num)),
            ip_template.replace('$', str(team_num) if ip_version == 4 else hex(team_num)[2:])
        )
        for team_num in range(start_num, start_num + count_teams)
    )

    pool = request.app['pool']

    async with pool.acquire() as conn:
        try:
            await conn.copy_records_to_table(
                table_name='t_teams',
                records=team_generator,
                columns=['team_name', 'team_ip']
            )
        except asyncpg.exceptions.UniqueViolationError:
            raise exceptions.TeamExists

    return web.json_response(
        {
            'status': 'ok'
        }
    )
