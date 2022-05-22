#! /usr/bin/env python

import asyncio
import gzip
import os
import pickle
import sys
import time
from datetime import datetime
from functools import wraps, lru_cache

import click
import matplotlib.pyplot as plt
import numpy as np
from dateutil import parser, tz
from frozendict import frozendict

from auth import Auth, MultiThread, requests_retry_session

RUNNING_AVERAGE = 50
AVERAGE_TIER = 11  # Silver 3


def file_to_object(save_file):
    print("Reading database")
    try:
        fp = gzip.open(save_file, 'rb')
        object = pickle.load(fp)
    except IOError as ioe:
        print(ioe.strerror)
        return None
    finally:
        if fp:
            fp.close()
    return object


def object_to_file(object, filename):
    print("Saving to database")
    try:
        if os.path.exists(f"{filename}.bak"):
            os.remove(f"{filename}.bak")
        if os.path.exists(filename):
            os.rename(filename, f"{filename}.bak")
        fp = gzip.open(filename, 'wb')
        pickle.dump(object, fp, protocol=2)
        if os.path.getsize(filename) == 0:
            print("Failed to save to database")
            os.remove(filename)
            if os.path.exists(f"{filename}.bak"):
                os.rename(f"{filename}.bak", filename)
    except BaseException as e:
        if fp:
            fp.close()
        print("Failed to save to database")
        os.remove(filename)
        if os.path.exists(f"{filename}.bak"):
            os.rename(f"{filename}.bak", filename)
    finally:
        fp.close()


def draw_progress_bar(percent, barLen=20):
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
    print("Logging in")
    a = Auth(username, password)
    asyncio.get_event_loop().run_until_complete(a.authenticate())
    return a


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
def get_user_id(auth):
    print("Getting user id")
    response = auth.session.post('https://auth.riotgames.com/userinfo', headers=auth.headers, json={}).json()
    user_id = response['sub']
    return user_id


@lru_cache
def get_maps():
    url = "https://valorant-api.com/v1/maps"
    response = requests_retry_session().get(url).json()
    return response.get('data', None)


@lru_cache
def get_map(map_url):
    maps = get_maps()
    map = next((m for m in maps if m['mapUrl'] == map_url), None)
    return map


@lru_cache
def get_agent(uuid=None):
    url = f"https://valorant-api.com/v1/agents/{uuid.lower()}"
    response = requests_retry_session().get(url).json()
    return response.get('data', None)


@lru_cache
def get_all_weapons():
    url = "https://valorant-api.com/v1/weapons"
    response = requests_retry_session().get(url).json()
    return response.get('data', [])


@lru_cache
def get_weapon(name=None, uuid=None):
    weapons = get_all_weapons()
    key = 'displayName' if name else 'uuid'
    value = name.title() if name else uuid.lower()

    weapon = next((w for w in weapons if w[key] == value), None)
    if not weapon:
        return get_weapon(name='vandal')
    return weapon


def get_game_history(auth, zone='eu', exclude=[]):
    print("Fetching matches")

    user_id = get_user_id(auth)

    match_ids = []

    startindex = 0
    size = 20
    endindex = startindex + size
    url = 'https://pd.{zone}.a.pvp.net/match-history/v1/history/{user_id}?startIndex={startindex}&endIndex={endindex}'
    # url = 'https://pd.{zone}.a.pvp.net/mmr/v1/players/{user_id}/competitiveupdates?startIndex={startindex}&endIndex={endindex}'
    response = auth.session.get(url.format(zone=zone, user_id=user_id, startindex=startindex, endindex=endindex),
                                headers=auth.headers).json()
    # Matches
    root = 'History'
    # root = 'Matches'
    while len(response.get(root, [])) == size:
        match_ids.extend([m.get('MatchID') for m in response.get(root, [])])
        startindex += size
        endindex += size
        response = auth.session.get(url.format(zone=zone, user_id=user_id, startindex=startindex, endindex=endindex),
                                    headers=auth.headers).json()
    match_ids.extend([m.get('MatchID') for m in response.get(root, [])])
    match_ids = [m for m in match_ids if m not in exclude]

    print("Found {count} new matches".format(count=len(match_ids)))

    matches = {}
    match_info = 'https://pd.{zone}.a.pvp.net/match-details/v1/matches/{match_id}'
    for i, mid in enumerate(match_ids):
        draw_progress_bar((i + 1) / len(match_ids))
        response = auth.session.get(match_info.format(zone=zone, match_id=mid), headers=auth.headers, timeout=5).json()
        # if response.get('matchInfo', {}).get('isRanked') and \
        #        response.get('matchInfo', {}).get('queueID') == queue:
        matches[mid] = response
    print("")
    for i, m in enumerate(matches.values()):
        draw_progress_bar((i + 1) / len(matches))
        for p in m.get('players'):
            insert_competitive_tier(auth, p)
    print("")
    return matches


def backfill_tiers(auth, matches, size=100, exclude=[]):
    print(f"Backfilling tiers for last {size} games")
    print(f"There are in total {len(matches.values())} games")
    if size > len(matches.values()):
        splice = matches.values()
    else:
        splice = list(reversed(matches.values()))[:size]
    args = []
    for m in splice:
        for p in m.get('players'):
            if p['subject'] not in exclude:
                args.append((auth, p))
    start = time.time()
    t = MultiThread(insert_competitive_tier, args, queue_results=True, maxThreads=100)
    t.start()
    t.join()
    end = time.time()
    print("Time elapsed:", end - start)


def process_comp_matches(matches, user_id):
    print("Processing competitive matches")
    games = []
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'competitive':
            continue
        ranks = []
        winning_team = next((t for t in match['teams'] if t['won'] is True), None)
        scores = match['teams'][0]['roundsWon'], match['teams'][0]['roundsPlayed'] - match['teams'][0]['roundsWon']
        starttime = datetime.utcfromtimestamp(match.get('matchInfo').get('gameStartMillis') / 1000).replace(
            tzinfo=tz.tzutc()).isoformat()
        map = get_map(match.get('matchInfo').get('mapId')).get('displayName')
        game = {'date': starttime,
                'map': map}
        for player in match.get('players', []):
            if player.get('subject') == user_id:
                game['agent'] = get_agent(player.get('characterId')).get('displayName')
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


def _get_main_weapon(match, user_id):
    player_stats = next(ps for ps in match['roundResults'][0]['playerStats'] if ps['subject'] == user_id)
    # print(player_stats)
    weapons = {}
    for k in player_stats.get('kills', []):
        weapon = k.get('finishingDamage', {}).get('damageItem')
        if not weapons.get(weapon):
            weapons[weapon] = 0
        weapons[weapon] += 1
    if not weapons:
        return "Unknown"
    main_weapon = max(weapons, key=weapons.get)
    return get_weapon(uuid=main_weapon).get('displayName')
    # return weaponmap.get(main_weapon, main_weapon)


def get_dm_weight(main_weapon, avg_tier):
    tier_damp = 1 / 22
    weapon_damp = 6000
    baseline_weapon = 'Vandal'

    tier_weight = (tier_damp + 1 / AVERAGE_TIER) / (tier_damp + 1 / avg_tier)
    # print(f"tier_weight for {avg_tier:.2f}: {tier_weight:.2f}")
    baseline_weapon_cost = get_weapon(name=baseline_weapon).get('shopData').get('cost')
    # print(main_weapon)
    main_weapon_cost = get_weapon(name=main_weapon).get('shopData').get('cost')
    weapon_weight = (weapon_damp + baseline_weapon_cost) / (weapon_damp + main_weapon_cost)
    # print(f"weapon_weight for {main_weapon}: {weapon_weight:.2f}")
    # print(f"total weight: {tier_weight * weapon_weight:.2f}")
    # print("")
    return tier_weight * weapon_weight


def process_dm_matches(auth, matches, user_id):
    print("Processing deathmatch games")
    games = []
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'deathmatch':
            continue
        main_weapon = _get_main_weapon(match, user_id)
        starttime = datetime.utcfromtimestamp(match.get('matchInfo').get('gameStartMillis') / 1000).replace(
            tzinfo=tz.tzutc()).isoformat()
        map = get_map(match.get('matchInfo').get('mapId')).get('displayName')
        game = {'date': starttime,
                'map': map,
                'weapon': main_weapon}
        tiers = [p.get('competitiveTier') or AVERAGE_TIER for p in match.get('players') if p.get('subject') != user_id]
        avg_tier = round(sum(tiers) / len(tiers) if len(tiers) else AVERAGE_TIER, 2)
        me = next(p for p in match.get('players') if p.get('subject') == user_id)
        game['agent'] = get_agent(me.get('characterId')).get('displayName')
        game['kills'] = me['stats']['kills']
        game['deaths'] = me['stats']['deaths']
        game['score'] = me['stats']['score']
        game['assists'] = me['stats']['assists']
        game['avg_tier'] = avg_tier
        # print(f"Average Tier: {rankmap.get(round(avg_tier))}")
        # print(main_weapon)
        game['performance'] = round(
            ((game['kills'] * 1 + game['assists'] * 0.25) * get_dm_weight(main_weapon, avg_tier)) / (
                game['deaths']), 2)
        game['kd'] = round(game['kills'] / game['deaths'], 2)
        games.append(game)
    return games


def insert_competitive_tier(auth, player, zone='eu'):
    if player.get('competitiveTier', 0) != 0:
        return
    user_id = player.get('subject')
    url = "https://pd.{zone}.a.pvp.net/mmr/v1/players/{user_id}/competitiveupdates?startIndex=0&endIndex=1&queue=competitive"
    response = auth.session.get(url.format(zone=zone, user_id=user_id), headers=auth.headers, timeout=5).json()
    previous_match_rank = next(iter(m.get("TierAfterUpdate", 0) for m in response.get('Matches', [])), 0)
    player['competitiveTier'] = previous_match_rank if previous_match_rank != 0 else AVERAGE_TIER


def print_dm_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    running_average = []
    for game in games:
        running_average.append(game['performance'])
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print("DEATHMATCH")
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print(game['weapon'])
        print("{}/{} - {}".format(game['kills'], game['deaths'], game['kd']))
        print(f"{rankmap.get(round(game['avg_tier']))} - {game['performance']}")
        if len(running_average) > RUNNING_AVERAGE:
            running_average = running_average[-RUNNING_AVERAGE:]
            print("Running average: {}".format(round(sum(running_average) / len(running_average), 2)))
        print("-----")


def print_comp_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    for game in games:
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print("RANKED")
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print("Result: " + game['result'])
        print("Rank: " + game['rank'])
        print("MMR: " + game['mmr'] + "+" + str(game['progress']))
        print("-----")


def plot_comp_games(username: str, games: list):
    games = sorted(games, key=lambda i: i['date'])
    if not games:
        return
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


def plot_dm_games(username, games, weapon=None, metric='kd'):
    games_w = {}
    if weapon:
        for g in games:
            _weapon = g.get('weapon')
            if not games_w.get(_weapon):
                games_w[_weapon] = []
            games_w[_weapon].append(g)
        plot_dm_games_for_weapon(username, games_w[weapon], weapon, metric)
    else:
        plot_dm_games_for_weapon(username, games, "all weapons", metric)


def plot_dm_games_for_weapon(username, games, weapon, metric='kd'):
    games = sorted(games, key=lambda i: i['date'])
    if not games:
        return
    metric_values = [g[metric] for g in games]
    ra = []

    running_avg = []
    for game in games:
        running_avg.append(game[metric])
        if len(running_avg) > RUNNING_AVERAGE:
            running_avg = running_avg[-RUNNING_AVERAGE:]
            ra.append(round(sum(running_avg) / len(running_avg), 2))
        else:
            ra.append(None)

    dates = [g['date'] for g in games]
    en_dates = [i for i, d in enumerate(dates)]
    # plt.scatter(dates, [g['kd'] for g in games], color='gray', label=f"kd")
    plt.scatter(dates, metric_values, color='blue', label=f"{metric}")
    plt.plot(dates, ra, color='orange', label="Running Average")
    if len(en_dates) > 1:
        z = np.polyfit(en_dates, metric_values, 1)
        p = np.poly1d(z)
        plt.plot(en_dates, p(en_dates), "r--", label=f"{metric} Trend")

    # plt.yticks(list(rankmap.keys()), list(rankmap.values()))
    plt.ylim(bottom=0)
    plt.xticks(dates, en_dates)
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(b=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel(metric)
    plt.legend()
    plt.title(f'Deathmatch {metric} for {username} with {weapon}'.format(username=username, weapon=weapon))
    plt.show()


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


@click.command()
@click.argument('username')  # help="Your riot login username. Not in-game user")
@click.argument('password')
@click.option('--zone', default='eu', help="Valorant zone (eu, na etc)")
@click.option('--plot/--no-plot', default=True, help='Plot the result')
@click.option('--print/--no-print', 'print_', default=False, help='Print the games to terminal')
@click.option('--db-name', default=None, help="Database name and path. Default is ./{username}.db")
@click.option('--weapon', default=None, help="Show dm stats for this weapon only",
              type=click.Choice([d.get('displayName').lower() for d in get_all_weapons()]))
@click.option('--backfill', default=None, help="Backfill tiers for old deathmatch games", type=int)
def valstats(username, password, zone, plot, print_, db_name, weapon, backfill):
    if db_name is None:
        db_name = username + '.db'
    weapon = weapon.title() if weapon else weapon
    auth = login(username, password)
    user_id = get_user_id(auth)
    matches = file_to_object(db_name) or {}
    if backfill:
        backfill_tiers(auth, matches, size=backfill, exclude=[user_id])
    new_matches = get_game_history(auth, zone, exclude=list(matches.keys()))
    # if new_matches:
    matches.update(new_matches)
    object_to_file(matches, db_name)
    comp_matches = process_comp_matches(matches, user_id)
    dm_matches = process_dm_matches(auth, matches, user_id)
    if print_:
        print_comp_games(comp_matches)
        print_dm_games(dm_matches)
    if plot:
        # plot_dm_games(username, dm_matches, weapon, 'kd')
        plot_dm_games(username, dm_matches, weapon, 'performance')
        plot_comp_games(username, comp_matches)


if __name__ == '__main__':
    valstats()
