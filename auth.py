import re
import aiohttp
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import requests

User_agent = 'RiotClient/43.0.1.4195386.4190634 rso-auth (Windows;10;;Professional, x64)'


def requests_retry_session(retries=5, backoff_factor=1, status_forcelist=(500, 502, 504), session=None,):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


class Auth:

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.headers = None
        self.session = requests_retry_session()

    async def authenticate(self):
        session = aiohttp.ClientSession()
        data = {
            'client_id': 'play-valorant-web-prod',
            'nonce': '1',
            'redirect_uri': 'https://playvalorant.com/opt_in',
            'response_type': 'token id_token',
            'scope': 'account openid',
        }

        headers = {'Content-Type': 'application/json', 'User-Agent': User_agent}

        await session.post('https://auth.riotgames.com/api/v1/authorization', json=data, headers=headers)

        data = {
            'type': 'auth',
            'username': self.username,
            'password': self.password
        }

        async with session.put('https://auth.riotgames.com/api/v1/authorization', json=data, headers=headers) as r:
            data = await r.json()

        pattern = re.compile(
            'access_token=((?:[a-zA-Z]|\d|\.|-|_)*).*id_token=((?:[a-zA-Z]|\d|\.|-|_)*).*expires_in=(\d*)')
        data = pattern.findall(data['response']['parameters']['uri'])[0]
        access_token = data[0]
        id_token = data[1]
        expires_in = data[2]

        headers = {
            'Authorization': f'Bearer {access_token}',
            'User-Agent': User_agent
        }
        async with session.post('https://entitlements.auth.riotgames.com/api/token/v1', headers=headers, json={}) as r:
            data = await r.json()
        entitlements_token = data['entitlements_token']

        async with session.post('https://auth.riotgames.com/userinfo', headers=headers, json={}) as r:
            data = await r.json()

        user_id = data['sub']
        # print(data)
        name = data['acct']['game_name']
        tagline = data['acct']['tag_line']
        IGN = f"{name}#{tagline}"

        # print('User ID: ' + user_id)
        headers['X-Riot-Entitlements-JWT'] = entitlements_token
        headers["X-Riot-ClientPlatform"] = "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"

        body = {"id_token": id_token}

        async with session.put('https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant', headers=headers,
                               json=body) as r:
            data = await r.json()
            region = data['affinities']['live']

        await session.close()
        self.headers = headers
        return user_id, headers, region, IGN
