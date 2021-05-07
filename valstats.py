import sys
import re
import requests
import matplotlib.pyplot as plt
import click
from dateutil import parser, tz
import pickle
from functools import wraps, lru_cache
from frozendict import frozendict
from datetime import datetime
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry


def file_to_object(save_file):
    print("Reading database")
    try:
        object = pickle.load(open(save_file, "rb"))
    except IOError as ioe:
        print(ioe.strerror)
        return None
    return object


def object_to_file(object, filename):
    print("Saving to database")
    pickle.dump(object, open(filename, "wb"), protocol=2)


def draw_progress_bar(percent, barLen = 20):
    sys.stdout.write("\r")
    progress = ""
    for i in range(barLen):
        if i < int(barLen * percent):
            progress += "="
        else:
            progress += " "
    sys.stdout.write("[ %s ] %.2f%%" % (progress, percent * 100))
    sys.stdout.flush()


def login(username, password):

    def _start_session():
        auth_url = 'https://auth.riotgames.com/api/v1/authorization'
        session = requests_retry_session()
        # Start authorization session
        data = {
            'client_id': 'play-valorant-web-prod',
            'nonce': '1',
            'redirect_uri': 'https://playvalorant.com/opt_in',
            'response_type': 'token id_token',
        }
        session.post(auth_url, json=data).json()
        return session

    def _get_auth_token(session, username, password):
        # Authorize and get access-token
        auth_url = 'https://auth.riotgames.com/api/v1/authorization'
        data = {
            'type': 'auth',
            'username': f'{username}',
            'password': f'{password}',
        }
        response = session.put(auth_url, json=data).json()
        # print(response)

        pattern = re.compile(
            'access_token=((?:[a-zA-Z]|\d|\.|-|_)*).*id_token=((?:[a-zA-Z]|\d|\.|-|_)*).*expires_in=(\d*)')
        response = pattern.findall(response['response']['parameters']['uri'])[0]
        access_token = response[0]
        # print('Access Token: ' + access_token)
        return access_token

    def _get_request_headers(session, auth_token):
        entitlement_url = 'https://entitlements.auth.riotgames.com/api/token/v1'
        headers = {
            'Authorization': f'Bearer {auth_token}',
            'X-Riot-ClientPlatform': 'ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0KCSJwbGF0Z'
                                     'm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0Ij'
                                     'ogIlVua25vd24iDQp9',
            'X-Riot-ClientVersion': 'release-02.01-shipping-6-511946',
        }
        response = session.post(entitlement_url, headers=headers, json={}).json()
        entitlements_token = response['entitlements_token']
        headers['X-Riot-Entitlements-JWT'] = entitlements_token
        # print('Entitlements Token: ' + entitlements_token)
        return headers

    print("Logging in")
    session = _start_session()
    auth_token = _get_auth_token(session, username, password)
    headers = _get_request_headers(session, auth_token)
    return session, headers


def freezeargs(func):
    """Transform mutable dictionnary
    Into immutable
    Useful to be compatible with cache
    """

    @wraps(func)
    def wrapped(*args, **kwargs):
        args = tuple([frozendict(arg) if isinstance(arg, dict) else arg for arg in args])
        kwargs = {k: frozendict(v) if isinstance(v, dict) else v for k, v in kwargs.items()}
        return func(*args, **kwargs)
    return wrapped


@freezeargs
@lru_cache
def get_user_id(session, headers):
    print("Getting user id")
    response = session.post('https://auth.riotgames.com/userinfo', headers=headers, json={}).json()
    user_id = response['sub']
    return user_id


def get_comp_history(session, headers, zone='eu'):
    print("Fetching matches")

    user_id = get_user_id(session, headers)

    match_ids = []

    startindex = 0
    size = 20
    endindex = startindex + size
    url = 'https://pd.{zone}.a.pvp.net/match-history/v1/history/{user_id}?startIndex={startindex}&endIndex={endindex}'
    # url = 'https://pd.{zone}.a.pvp.net/mmr/v1/players/{user_id}/competitiveupdates?startIndex={startindex}&endIndex={endindex}'
    response = session.get(url.format(zone=zone, user_id=user_id, startindex=startindex, endindex=endindex),
                           headers=headers).json()
    # Matches
    root = 'History'
    # root = 'Matches'
    while len(response.get(root, [])) == size:
        match_ids.extend([m.get('MatchID') for m in response.get(root, [])])
        startindex += size
        endindex += size
        response = session.get(url.format(zone=zone, user_id=user_id, startindex=startindex, endindex=endindex),
                               headers=headers).json()
    match_ids.extend([m.get('MatchID') for m in response.get(root, [])])

    print("Found {count} matches".format(count=len(match_ids)))

    matches = {}
    match_info = 'https://pd.{zone}.a.pvp.net/match-details/v1/matches/{match_id}'
    for i, mid in enumerate(match_ids):
        draw_progress_bar((i + 1) / len(match_ids))
        response = session.get(match_info.format(zone=zone, match_id=mid), headers=headers, timeout=5).json()
        if response.get('matchInfo', {}).get('isRanked'):
            matches[mid] = response
    print('')
    print("Found {count} ranked matches".format(count=len(matches.keys())))
    return matches


def process_comp_matches(matches, user_id):
    print("Processing matches")
    games = []
    for match in matches.values():
        if match.get('matchInfo', {}).get('queueID') != 'competitive':
            continue
        ranks = []
        winning_team = next((t for t in match['teams'] if t['won'] is True), None)
        starttime = datetime.utcfromtimestamp(match.get('matchInfo').get('gameStartMillis') / 1000).replace(tzinfo=tz.tzutc()).isoformat()
        map = mapmap[match.get('matchInfo').get('mapId').split('/')[-1:][0]]
        game = {'date': starttime,
                'map': map}
        for player in match.get('players', []):
            if player.get('subject') == user_id:
                game['agent'] = agentmap.get(player.get('characterId'), player.get('characterId'))
                game['rank'] = rankmap[player.get('competitiveTier')]
                game['rank_raw'] = player.get('competitiveTier')
                if not winning_team:
                    game['result'] = 'Draw'
                elif player['teamId'] == winning_team['teamId']:
                    game['result'] = 'Win'
                else:
                    game['result'] = 'Loss'
            if player.get('competitiveTier', 0) != 0 and player.get('subject') != user_id:
                ranks.append(player.get('competitiveTier'))
        avg = sum(ranks) / len(ranks)
        game['mmr'] = rankmap[int(avg)]
        game['mmr_raw'] = avg
        game['progress'] = int((avg - int(avg)) * 100)
        games.append(game)
    return games


def print_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    for game in games:
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print("Result: " + game['result'])
        print("Rank: " + game['rank'])
        print("MMR: " + game['mmr'] + "+" + str(game['progress']))
        print("-----")


def plot_games(username: str, games: list):
    games = sorted(games, key=lambda i: i['date'])
    mmr = [g['mmr_raw'] for g in games]
    ranks = [g['rank_raw'] for g in games]
    dates = [g['date'] for g in games]

    plt.plot(dates, mmr, label="Est. MMR")
    plt.plot(dates, ranks, label="Rank")
    plt.yticks(list(rankmap.keys()), list(rankmap.values()))
    plt.xticks(dates, [i for i, d in enumerate(dates)])
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(b=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel('Rank')

    plt.legend()
    plt.title('RR vs MMR for {username}'.format(username=username))

    plt.show()


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


actmap = {
    '1.1': "3f61c772-4560-cd3f-5d3f-a7ab5abda6b3",
    '1.2': "0530b9c4-4980-f2ee-df5d-09864cd00542",
    '1.3': "46ea6166-4573-1128-9cea-60a15640059b",
    '2.1': "97b6e739-44cc-ffa7-49ad-398ba502ceb0",
    '2.2': "ab57ef51-4e59-da91-cc8d-51a5a2b9b8ff",
    '2.3': "52e9749a-429b-7060-99fe-4595426a0cf7",
}

rankmap = {
    0: 'Unranked',
    3: 'Iron 1',
    4: 'Iron 2',
    5: 'Iron 3',
    6: 'Bronze 1',
    7: 'Bronze 2',
    8: 'Bronze 3',
    9: 'Silver 1',
    10: 'Silver 2',
    11: 'Silver 3',
    12: 'Gold 1',
    13: 'Gold 2',
    14: 'Gold 3',
    15: 'Platinum 1',
    16: 'Platinum 2',
    17: 'Platinum 3',
    18: 'Diamond 1',
    19: 'Diamond 2',
    20: 'Diamond 3',
}

mapmap = {
    'Duality': 'Bind',
    'Port': 'Icebox',
    'Triad': 'Haven',
    'Bonsai': 'Split',
    'Ascent': 'Ascent',
    'Foxtrot': 'Breeze',
}

agentmap = {
    '1e58de9c-4950-5125-93e9-a0aee9f98746': 'Killjoy',
    '9f0d8ba9-4140-b941-57d3-a7ad57c6b417': 'Brimstone',
    '7f94d92c-4234-0a36-9646-3a87eb8b5c89': 'Yoru',
    '5f8d3a7f-467b-97f3-062c-13acf203c006': 'Breach',
    'eb93336a-449b-9c1b-0a54-a891f7921d69': 'Phoenix',
    '707eab51-4836-f488-046a-cda6bf494859': 'Viper',
    'f94c3b30-42be-e959-889c-5aa313dba261': 'Raze',
    '6f2a04ca-43e0-be17-7f36-b3908627744d': 'Skye',
    '117ed9e3-49f3-6512-3ccf-0cada7e3823b': 'Cypher',
    'ded3520f-4264-bfed-162d-b080e2abccf9': 'Sova',
    '320b2a48-4d9b-a075-30f1-1f93a9b638fa': 'Sova',
    '41fb69c1-4189-7b37-f117-bcaf1e96f1bf': 'Astra',
    '569fdd95-4d10-43ab-ca70-79becc718b46': 'Sage',
    'a3bfb853-43b2-7238-a4f1-ad90e9e46bcc': 'Reyna',
    '8e253930-4c05-31dd-1b6c-968525494517': 'Omen',
    'add6443a-41bd-e414-f6ad-e58d267f4e95': 'Jett',
}


@click.command()
@click.argument('username')  # help="Your riot login username. Not in-game user")
@click.argument('password')
@click.option('--zone', default='eu', help="Valorant zone (eu, na etc)")
@click.option('--plot/--no-plot', default=True, help='Plot the result')
@click.option('--print/--no-print', 'print_', default=True, help='Print the games to terminal')
@click.option('--db-name', default=None, help="Database name and path. Default is ./{username}.db")
def valstats(username, password, zone, plot, print_, db_name):
    if db_name is None:
        db_name = username + '.db'
    session, headers = login(username, password)
    user_id = get_user_id(session, headers)
    matches = file_to_object(db_name) or {}
    new_matches = get_comp_history(session, headers, zone)
    matches.update(new_matches)
    object_to_file(matches, db_name)
    matches = process_comp_matches(matches, user_id)
    if print_:
        print_games(matches)
    if plot:
        plot_games(username, matches)


if __name__ == '__main__':
    valstats()
