#! /usr/bin/env python3

import numpy as np
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
import asyncio
from auth import Auth

RUNNING_AVERAGE = 50


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


async def login(username, password):
    print("Logging in")
    a = Auth(username, password)
    session = requests_retry_session()
    puuid, headers, region, ign = await a.authenticate()
    return session, headers


def freezeargs(func):
    """Transform mutable dictionary
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


def get_game_history(session, headers, zone='eu', exclude=[]):
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
    match_ids = [m for m in match_ids if m not in exclude]

    print("Found {count} new matches".format(count=len(match_ids)))

    matches = {}
    match_info = 'https://pd.{zone}.a.pvp.net/match-details/v1/matches/{match_id}'
    for i, mid in enumerate(match_ids):
        draw_progress_bar((i + 1) / len(match_ids))
        response = session.get(match_info.format(zone=zone, match_id=mid), headers=headers, timeout=5).json()
        #if response.get('matchInfo', {}).get('isRanked') and \
        #        response.get('matchInfo', {}).get('queueID') == queue:
        matches[mid] = response
    print('')
    print("Found {count} new matches".format(count=len(matches.keys())))
    return matches


def process_comp_matches(matches, user_id):
    print("Processing competitive matches")
    games = []
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'competitive':
            continue
        ranks = []
        winning_team = next((t for t in match['teams'] if t['won'] is True), None)
        scores = match['teams'][0]['roundsWon'], match['teams'][0]['roundsPlayed'] - match['teams'][0]['roundsWon']
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
                    game['result'] = 'Draw {w}-{l}'.format(w=max(scores), l=min(scores))
                elif player['teamId'] == winning_team['teamId']:
                    game['result'] = 'Win {w}-{l}'.format(w=max(scores), l=min(scores))
                else:
                    game['result'] = 'Loss {l}-{w}'.format(w=max(scores), l=min(scores))
            if player.get('competitiveTier', 0) != 0 and player.get('subject') != user_id:
                ranks.append(player.get('competitiveTier'))
        avg = sum(ranks) / len(ranks)
        game['mmr'] = rankmap[int(avg)]
        game['mmr_raw'] = avg
        game['progress'] = int((avg - int(avg)) * 100)
        games.append(game)
    return games


def process_dm_matches(matches, user_id):
    print("Processing deathmatch games")
    games = []
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'deathmatch':
            continue
        starttime = datetime.utcfromtimestamp(match.get('matchInfo').get('gameStartMillis') / 1000).replace(
            tzinfo=tz.tzutc()).isoformat()
        map = mapmap[match.get('matchInfo').get('mapId').split('/')[-1:][0]]
        game = {'date': starttime,
                'map': map}
        me = next(p for p in match.get('players') if p.get('subject') == user_id)
        game['agent'] = agentmap.get(me.get('characterId'), me.get('characterId'))
        game['kills'] = me['stats']['kills']
        game['deaths'] = me['stats']['deaths']
        game['score'] = me['stats']['score']
        game['kd'] = round(game['kills'] / game['deaths'], 2)
        games.append(game)
    return games


def print_dm_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    running_average = []
    for game in games:
        running_average.append(game['kd'])
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print("{}/{} - {}".format(game['kills'], game['deaths'], game['kd']))
        if len(running_average) > RUNNING_AVERAGE:
            running_average = running_average[-RUNNING_AVERAGE:]
            print("Running average: {}".format(round(sum(running_average) / len(running_average), 2)))
        print("-----")


def print_comp_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    for game in games:
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print("Result: " + game['result'])
        print("Rank: " + game['rank'])
        print("MMR: " + game['mmr'] + "+" + str(game['progress']))
        print("-----")


def plot_comp_games(username: str, games: list):
    games = sorted(games, key=lambda i: i['date'])
    mmr = [g['mmr_raw'] for g in games]
    ranks = [g['rank_raw'] for g in games]
    dates = [g['date'] for g in games]
    en_dates = [i for i, d in enumerate(dates)]

    plt.plot(dates, mmr, label="Est. MMR")
    plt.plot(dates, ranks, label="Rank")
    z = np.polyfit(en_dates, mmr, 1)
    p = np.poly1d(z)
    plt.plot(en_dates, p(en_dates), "r--", label="Rank Trend")
    plt.yticks(list(rankmap.keys()), list(rankmap.values()))
    plt.xticks(dates, en_dates)
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(b=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel('Rank')

    plt.legend()
    plt.title('RR vs MMR for {username}'.format(username=username))

    plt.show()


def plot_dm_games(username, games):
    games = sorted(games, key=lambda i: i['date'])
    kd = [g['kd'] for g in games]
    ra = []

    running_avg = []
    for game in games:
        running_avg.append(game['kd'])
        if len(running_avg) > RUNNING_AVERAGE:
            running_avg = running_avg[-RUNNING_AVERAGE:]
            ra.append(round(sum(running_avg) / len(running_avg), 2))
        else:
            ra.append(None)

    dates = [g['date'] for g in games]
    en_dates = [i for i, d in enumerate(dates)]
    plt.scatter(dates, kd, color='blue', label="K/D")
    plt.plot(dates, ra, color='orange', label="Running Average")
    z = np.polyfit(en_dates, kd, 1)
    p = np.poly1d(z)
    plt.plot(en_dates, p(en_dates), "r--", label="K/D Trend")

    # plt.yticks(list(rankmap.keys()), list(rankmap.values()))
    plt.ylim(bottom=0)
    plt.xticks(dates, en_dates)
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(b=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel('K/D')
    plt.legend()
    plt.title('Deathmatch K/D for {username}'.format(username=username))
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
    21: 'Immortal 1',
    22: 'Immortal 2',
    23: 'Immortal 3',
    24: 'Radiant'
}

mapmap = {
    'Duality': 'Bind',
    'Port': 'Icebox',
    'Triad': 'Haven',
    'Bonsai': 'Split',
    'Ascent': 'Ascent',
    'Foxtrot': 'Breeze',
    'Canyon': 'Fracture',
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
    '601dbbe7-43ce-be57-2a40-4abd24953621': 'Kay/O',
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
    session, headers = asyncio.run(login(username, password))
    user_id = get_user_id(session, headers)
    matches = file_to_object(db_name) or {}
    new_matches = get_game_history(session, headers, zone, exclude=list(matches.keys()))
    matches.update(new_matches)
    object_to_file(matches, db_name)
    comp_matches = process_comp_matches(matches, user_id)
    dm_matches = process_dm_matches(matches, user_id)
    if print_:
        print_dm_games(dm_matches)
        print_comp_games(comp_matches)
    if plot:
        plot_dm_games(username, dm_matches)
        plot_comp_games(username, comp_matches)


if __name__ == '__main__':
    valstats()
