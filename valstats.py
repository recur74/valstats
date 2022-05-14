#! /usr/bin/env python

import asyncio
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

from auth import Auth, MultiThread

RUNNING_AVERAGE = 50
AVERAGE_TIER = 11  # Silver 3


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
        #if response.get('matchInfo', {}).get('isRanked') and \
        #        response.get('matchInfo', {}).get('queueID') == queue:
        matches[mid] = response
    print("")
    for i, m in enumerate(matches.values()):
        draw_progress_bar((i + 1) / len(matches))
        for p in m.get('players'):
            insert_competitive_tier(auth, p)
    print("")
    return matches


def backfill_tiers(auth, matches, size=100):
    print(f"Backfilling tiers for last {size} games")
    print(f"There are in total {len(matches.values())} games")
    if size > len(matches.values()):
        splice = matches.values()
    else:
        splice = list(reversed(matches.values()))[:size]
    args = []
    for m in splice:
        for p in m.get('players'):
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
    return weaponmap.get(main_weapon, main_weapon)


def process_dm_matches(auth, matches, user_id):
    print("Processing deathmatch games")
    games = []
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'deathmatch':
            continue
        main_weapon = _get_main_weapon(match, user_id)
        starttime = datetime.utcfromtimestamp(match.get('matchInfo').get('gameStartMillis') / 1000).replace(
            tzinfo=tz.tzutc()).isoformat()
        map = mapmap[match.get('matchInfo').get('mapId').split('/')[-1:][0]]
        game = {'date': starttime,
                'map': map,
                'weapon': main_weapon}
        tiers = [p.get('competitiveTier') or AVERAGE_TIER for p in match.get('players') if p.get('subject') != user_id]
        avg_tier = sum(tiers) / len(tiers) if len(tiers) else AVERAGE_TIER
        me = next(p for p in match.get('players') if p.get('subject') == user_id)
        game['agent'] = agentmap.get(me.get('characterId'), me.get('characterId'))
        game['kills'] = me['stats']['kills']
        game['deaths'] = me['stats']['deaths']
        game['score'] = me['stats']['score']
        game['assists'] = me['stats']['assists']
        game['avg_tier'] = avg_tier
        game['performance'] = round(((game['kills'] * 0.75 + game['assists'] * 0.25) * avg_tier) / (game['deaths']), 2)
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
        running_average.append(game['kd'])
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print(game['weapon'])
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
    plt.scatter(dates, metric_values, color='blue', label=f"[{metric}]")
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
    '22697a3d-45bf-8dd7-4fec-84a9e28c69d7': 'Chamber',
    'bb2a4828-46eb-8cd1-e765-15848195d751': 'Neon',
    'dade69b4-4f5a-8528-247b-219e5a1facd6': 'Fade',
}

weaponmap = {
    '9C82E19D-4575-0200-1A81-3EACF00CF872': 'Vandal',
    'EE8E8D15-496B-07AC-E5F6-8FAE5D4C7B1A': 'Phantom',
    'E336C6B8-418D-9340-D77F-7A9E4CFE0702': 'Sheriff',
    'AE3DE142-4D85-2547-DD26-4E90BED35CF7': 'Bulldog',
    '4ADE7FAA-4CF1-8376-95EF-39884480959B': 'Guardian',
    'C4883E50-4494-202C-3EC3-6B8A9284F00B': 'Marshal',
    'A03B24D3-4319-996D-0F8C-94BBFBA1DFC7': 'Operator',
    '29A0CFAB-485B-F5D5-779A-B59F85E204A8': 'Classic',
    '1BAA85B4-4C70-1284-64BB-6481DFC3BB4E': 'Ghost',

}


@click.command()
@click.argument('username')  # help="Your riot login username. Not in-game user")
@click.argument('password')
@click.option('--zone', default='eu', help="Valorant zone (eu, na etc)")
@click.option('--plot/--no-plot', default=True, help='Plot the result')
@click.option('--print/--no-print', 'print_', default=True, help='Print the games to terminal')
@click.option('--db-name', default=None, help="Database name and path. Default is ./{username}.db")
@click.option('--weapon', default=None, help="Show dm stats for this weapon only", type=click.Choice([w.lower() for w in weaponmap.values()]))
@click.option('--backfill', default=None, help="Backfill tiers for old deathmatch games", type=int)
def valstats(username, password, zone, plot, print_, db_name, weapon, backfill):
    if db_name is None:
        db_name = username + '.db'
    weapon = weapon.title() if weapon else weapon
    auth = login(username, password)
    user_id = get_user_id(auth)
    matches = file_to_object(db_name) or {}
    if backfill:
        backfill_tiers(auth, matches, size=backfill)
    new_matches = get_game_history(auth, zone, exclude=list(matches.keys()))
    matches.update(new_matches)
    object_to_file(matches, db_name)
    comp_matches = process_comp_matches(matches, user_id)
    dm_matches = process_dm_matches(auth, matches, user_id)
    if print_:
        print_dm_games(dm_matches)
        print_comp_games(comp_matches)
    if plot:
        plot_dm_games(username, dm_matches, weapon, 'kd')
        plot_dm_games(username, dm_matches, weapon, 'performance')
        plot_comp_games(username, comp_matches)


if __name__ == '__main__':
    valstats()
