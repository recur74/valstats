#! /usr/bin/env python
import copy
import json
from datetime import datetime, date
from functools import lru_cache
from statistics import median

import click
import matplotlib.pyplot as plt
import numpy as np
from dateutil import parser, tz

import elo
from auth import Auth, requests_retry_session
from database import file_to_object, get_session, Match, User

RUNNING_AVERAGE = 50
AVERAGE_TIER = 12  # Gold 1

HENRIK_API = "https://api.henrikdev.xyz/valorant"
auth = None

plt.rcParams['ytick.right'] = plt.rcParams['ytick.labelright'] = True
plt.rcParams['ytick.left'] = plt.rcParams['ytick.labelleft'] = False

global_elo_map = {
    3: 1034, 4: 1039, 5: 1044,
    6: 1093, 7: 1098, 8: 1103,
    9: 1108, 10: 1117, 11: 1126,
    12: 1131, 13: 1136, 14: 1141,
    15: 1146, 16: 1151, 17: 1156,
    18: 1161, 19: 1166, 20: 1173,
    21: 1182, 22: 1187, 23: 1198,
    24: 1203, 25: 1224, 26: 1234,
    27: 1318
}


def get_tier_elo(tier, elo_map):
    return elo_map.get(tier)


@lru_cache
def get_user_id(session):
    print("Getting user id", flush=True)
    user_id = session.query(User.id). \
        filter(User.name == auth.name). \
        filter(User.tag == auth.tag). \
        one_or_none()
    if user_id:
        return user_id[0]
    url = f"{HENRIK_API}/v1/account/{auth.name}/{auth.tag}"
    response = auth.session.get(url).json()
    if response.get('status') == 404:
        print(f"Could not find user '{auth.name}#{auth.tag}'")
        return None
    if response.get('status') != 200:
        print(response.get('errors')[0]['message'])
        return None
    user_id = response['data']['puuid']
    session.add(User(id=user_id, name=auth.name, tag=auth.tag))
    session.commit()
    return user_id


def get_user_mmr(user_id):
    url = f"{HENRIK_API}/v2/by-puuid/mmr/{auth.region}/{user_id}"
    response = auth.session.get(url).json()
    if response['status'] != 200:
        return AVERAGE_TIER
    return response['data']['current_data']['currenttier']


@lru_cache
def get_tier_by_number(number):
    tiers = get_competitive_tiers()
    return next(t for t in tiers if t.get('tier') == number)


@lru_cache
def get_competitive_tiers():
    url = "https://valorant-api.com/v1/competitivetiers"
    response = requests_retry_session().get(url).json()
    current_episode = response.get('data')[-1]
    tiers = current_episode.get('tiers')
    for t in tiers:
        t['tierName'] = t['tierName'].title()
    return current_episode.get('tiers')


@lru_cache
def get_maps():
    url = "https://valorant-api.com/v1/maps"
    response = requests_retry_session().get(url).json()
    return response.get('data', None)


@lru_cache
def get_map(map_url):
    maps = get_maps()
    map = next((m for m in maps if m['mapUrl'] == map_url or m['displayName'] == map_url), None)
    return map


@lru_cache
def get_agents():
    url = f"https://valorant-api.com/v1/agents"
    response = requests_retry_session().get(url).json()
    return response.get('data', None)


@lru_cache
def get_agent(uuid_or_name=None):
    agents = get_agents()
    agent = next((m for m in agents if m['uuid'] == uuid_or_name or m['displayName'] == uuid_or_name), None)
    return agent


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


def map_to_internal(matches):
    internal = {}
    for m in matches:
        internal[m['metadata']['matchid']] = m
        m['matchInfo'] = m['metadata']
        m['matchInfo']['matchId'] = m['metadata']['matchid']
        m['matchInfo']['queueID'] = m['metadata']['mode'].lower()
        m['matchInfo']['gameStartMillis'] = m['metadata']['game_start'] * 1000
        m['matchInfo']['mapId'] = m['metadata']['map']
        if m['matchInfo']['queueID'] == 'competitive':
            m['teams'] = [
                {'teamId': k, 'won': v['has_won'], 'roundsPlayed': v['rounds_won'] + v['rounds_lost'],
                 'roundsWon': v['rounds_won']} for k, v in m['teams'].items()
            ]
        m['players'] = [
            {k: v for k, v in p.items()} for p in m['players']['all_players']
        ]
        for p in m['players']:
            p['subject'] = p['puuid']
            p['competitiveTier'] = p['currenttier']
            p['characterId'] = p['character']
            p['teamId'] = p['team'].lower()
        for k in m['kills']:
            k['killer'] = k['killer_puuid']
            k['victim'] = k['victim_puuid']
            k['finishingDamage'] = {}
            k['finishingDamage']['damageItem'] = k['damage_weapon_id']
    return internal


def get_game_history(session, exclude=[]):
    print("Fetching matches", flush=True)
    user_id = get_user_id(session)
    url = f"{HENRIK_API}/v3/by-puuid/matches/{auth.region}/{user_id}"
    result = []
    for typ in ('deathmatch', 'competitive'):
        response = auth.session.get(url, params={'size': 10, 'filter': typ}).json()
        for m in response['data']:
            if m['metadata']['matchid'] not in exclude:
                if typ == 'deathmatch':
                    insert_competitive_tiers(m)
                result.append(m)
    print(f"Found {len(result)} new games", flush=True)
    return map_to_internal(result)


def insert_competitive_tiers(deathmatch):
    for p in deathmatch['players']['all_players']:
        tier = get_user_mmr(p['puuid'])
        p['currenttier'] = tier


def process_comp_matches(matches, user_id):
    print("Processing competitive matches", flush=True)
    games = []
    last_rank = AVERAGE_TIER
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
                game['rank'] = get_tier_by_number(player.get('competitiveTier')).get('tierName')
                game['rank_raw'] = player.get('competitiveTier')
                if not winning_team:
                    game['result'] = 'Draw {w}-{l}'.format(w=max(scores), l=min(scores))
                elif player['teamId'] == winning_team['teamId']:
                    game['result'] = 'Win {w}-{l}'.format(w=max(scores), l=min(scores))
                else:
                    game['result'] = 'Loss {l}-{w}'.format(w=max(scores), l=min(scores))
            if player.get('competitiveTier', 0) != 0 and player.get('subject') != user_id:
                ranks.append(player.get('competitiveTier'))
        avg = sum(ranks) / len(ranks) if len(ranks) != 0 else last_rank
        last_rank = avg
        game['mmr'] = get_tier_by_number(int(avg)).get('tierName')
        game['mmr_raw'] = avg
        game['progress'] = int((avg - int(avg)) * 100)
        games.append(game)
    return games


def get_main_weapon(match, user_id):
    # player_stats = next(ps for ps in match['roundResults'][0]['playerStats'] if ps['subject'] == user_id)
    # print(player_stats)
    user_kills = [k for k in match['kills'] if k['killer'] == user_id]
    weapons = {}
    for k in user_kills:
        weapon = k.get('finishingDamage', {}).get('damageItem')
        if not weapons.get(weapon):
            weapons[weapon] = 0
        weapons[weapon] += 1
    if not weapons:
        return "Unknown"
    main_weapon = max(weapons, key=weapons.get)
    return get_weapon(uuid=main_weapon).get('displayName')
    # return weaponmap.get(main_weapon, main_weapon)


def get_dm_weight(main_weapon, avg_tier, date_of_match):
    tier_damp = AVERAGE_TIER
    weapon_damp = 6000
    baseline_weapon = 'Vandal'
    days_ago = (date.today() - parser.parse(date_of_match).date()).days
    tier_decay = (days_ago - 3650) / -3650

    tier_weight = (avg_tier * tier_decay + tier_damp) / (AVERAGE_TIER + tier_damp)
    # print(f"tier_weight for {avg_tier:.2f}: {tier_weight:.2f}")
    baseline_weapon_cost = get_weapon(name=baseline_weapon).get('shopData').get('cost')
    # print(main_weapon)
    main_weapon_cost = get_weapon(name=main_weapon).get('shopData').get('cost')
    weapon_weight = (weapon_damp + baseline_weapon_cost) / (weapon_damp + main_weapon_cost)
    # print(f"weapon_weight for {main_weapon}: {weapon_weight:.2f}")
    # print(f"total weight: {tier_weight * weapon_weight:.2f}")
    # print("")
    return tier_weight * weapon_weight


def elo_gain_for_match_for_user(match, user_id, elo_map=global_elo_map,
                                initial_elo=get_tier_elo(AVERAGE_TIER, global_elo_map)):
    match_elo_score = {'Unknown': {'expected': 0, 'actual': 0}}
    main_weapon = get_main_weapon(match, user_id)
    for kill in match['kills']:
        if 'victim' not in kill:
            kill['victim'] = kill['victim_puuid']
        if kill['killer'] != user_id and kill['victim'] != user_id:
            continue
        opponent_uuid = next(
            iter(u for u in [kill.get('victim'), kill.get('killer')] if u != user_id and u is not None), None)
        if opponent_uuid is None:
            continue
        opponent_main_weapon = get_main_weapon(match, opponent_uuid)
        kill_weapon = get_weapon(uuid=kill.get('finishingDamage', {}).get('damageItem')).get('displayName')
        if kill_weapon != main_weapon or opponent_main_weapon != main_weapon:
            continue
        if not match_elo_score.get(kill_weapon):
            match_elo_score[kill_weapon] = {'expected': 0, 'actual': 0}
        if kill['killer'] == user_id:
            victim = next(iter(p for p in match['players'] if p['subject'] == kill.get('victim')))
            opponent_tier = victim.get('competitiveTier')
            if opponent_tier is None:
                continue
            match_elo_score[kill_weapon]['expected'] += elo.expected(initial_elo, get_tier_elo(opponent_tier, elo_map))
            match_elo_score[kill_weapon]['actual'] += 1
        if kill.get('victim') == user_id:
            opponent_tier = next(
                iter(p.get('competitiveTier') for p in match['players'] if p['subject'] == kill['killer']))
            if opponent_tier is None:
                continue
            match_elo_score[kill_weapon]['expected'] += elo.expected(initial_elo, get_tier_elo(opponent_tier, elo_map))
    if main_weapon not in match_elo_score or len(match_elo_score[main_weapon]) == 0:
        return 0
    new_elo = elo.elo(initial_elo, match_elo_score[main_weapon]['expected'],
                      match_elo_score[main_weapon]['actual'], k=1)
    return new_elo - initial_elo


def process_dms_for_elo(matches, user_id):
    print("Processing deathmatch games for elo", flush=True)
    elos = {'Unknown': [0]}
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'deathmatch':
            continue
        main_weapon = get_main_weapon(match, user_id)
        if main_weapon not in elos:
            elos[main_weapon] = [get_tier_elo(AVERAGE_TIER, global_elo_map)]
        elo_gain = elo_gain_for_match_for_user(match=match,
                                               user_id=user_id,
                                               elo_map=global_elo_map,
                                               initial_elo=elos.get(main_weapon)[-1])
        elos[main_weapon].append(elos.get(main_weapon)[-1] + elo_gain)
    return elos


def _calibration_score(matches, elo_map, tier=AVERAGE_TIER, weapon='Vandal'):
    res = 0
    for match in matches.values():
        match_res = 0
        if match['matchInfo']['queueID'] != 'deathmatch':
            continue
        players = [p for p in match.get('players') if
                   p.get('competitiveTier') == tier and
                   get_main_weapon(match, p.get('subject')) == weapon]
        if len(players) < 2:
            continue
        for player in players:
            match_res += elo_gain_for_match_for_user(match, player.get('subject'), elo_map, get_tier_elo(tier, elo_map))
        res += match_res / len(players)
    return res


def _score_all_tiers(matches, elo_map):
    result = {'total': 0, 'tiers': {}}
    for tier in elo_map.keys():
        result['tiers'][tier] = _calibration_score(matches, elo_map, tier)
    result['total'] = sum([abs(v) for v in result['tiers'].values()])
    return result


def _adjust_elo(tier, amount, min_diff, elo_map=global_elo_map):
    new_elo_map = copy.copy(elo_map)
    MIN_TIER = min([k for k in global_elo_map.keys()])
    MAX_TIER = max([k for k in global_elo_map.keys()])
    new_elo_map[tier] += amount
    if amount > 0 and tier < MAX_TIER and new_elo_map[tier] + min_diff > new_elo_map[tier + 1]:
        new_elo_map = _adjust_elo(tier + 1, 1, min_diff, elo_map=new_elo_map)
    if amount < 0 and tier > MIN_TIER and elo_map[tier] - min_diff < elo_map[tier - 1]:
        new_elo_map = _adjust_elo(tier - 1, -1, min_diff, elo_map=new_elo_map)
    return new_elo_map


def calibrate_elo(matches, init_elo_map):
    NUDGE_DISTANCE = 1
    MIN_TIER_DIFF = 5
    best_elo_map = copy.copy(init_elo_map)
    scores = _score_all_tiers(matches, init_elo_map)

    last_score = scores['total']
    best_score = None
    iteration = 1
    while best_score is None or best_score < last_score:
        print(f"Iteration {iteration}")
        print(f"Current best elo-map {best_elo_map}")
        last_score = best_score if best_score is not None else last_score
        test_scores = {}
        for tier in init_elo_map.keys():
            print(f"Checking tier {get_tier_by_number(tier).get('tierName')}({tier})")
            score = scores['tiers'][tier]
            if score == 0:
                continue
            test_elo_map = _adjust_elo(tier=tier, amount=NUDGE_DISTANCE, min_diff=MIN_TIER_DIFF, elo_map=best_elo_map)
            test_scores[tier] = _score_all_tiers(matches, test_elo_map)['total']
        smallest = sorted(test_scores, key=lambda y: abs(test_scores[y]))[0]
        print(f"The best change was {get_tier_by_number(smallest).get('tierName')} with {test_scores[smallest]}")
        best_elo_map = _adjust_elo(tier=smallest, amount=NUDGE_DISTANCE, min_diff=MIN_TIER_DIFF, elo_map=best_elo_map)
        best_score = test_scores[smallest]
        iteration += 1


def process_dm_matches(auth, matches, user_id):
    print("Processing deathmatch games", flush=True)
    games = []
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'deathmatch':
            continue
        main_weapon = get_main_weapon(match, user_id)
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
        # print(f"Average Tier: {get_tier_by_number(round(avg_tier)).get('tierName')}")
        # print(main_weapon)
        game['performance'] = round(
            ((game['kills'] * 1 + game['assists'] * 0.25) * get_dm_weight(main_weapon, avg_tier, starttime)) / (
                game['deaths']), 2)
        game['kd'] = round(game['kills'] / game['deaths'], 2)
        games.append(game)
    return games


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
        print(f"{get_tier_by_number(round(game['avg_tier'])).get('tierName')} - {game['performance']}")
        if len(running_average) > RUNNING_AVERAGE:
            running_average = running_average[-RUNNING_AVERAGE:]
            # print("Running average: {}".format(round(sum(running_average) / len(running_average), 2)))
            print("Running median: {}".format(round(median(running_average), 2)))
        print("-----", flush=True)


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
        print("-----", flush=True)


def plot_comp_games(username: str, games: list):
    games = sorted(games, key=lambda i: i['date'])
    if not games:
        return
    mmr = [g['mmr_raw'] for g in games]
    ranks = [g['rank_raw'] for g in games]
    dates = [g['date'] for g in games]
    en_dates = [i for i, d in enumerate(dates)]

    plt.figure()
    plt.plot(dates, mmr, label="Est. MMR")
    plt.plot(dates, ranks, label="Rank")
    z = np.polyfit(en_dates, mmr, 1)
    p = np.poly1d(z)
    plt.plot(en_dates, p(en_dates), "r--", label="Rank Trend")
    plt.yticks(list(t.get('tier') for t in get_competitive_tiers()),
               list(t.get('tierName') for t in get_competitive_tiers()))
    plt.xticks(dates, en_dates)
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(b=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel('Rank')

    plt.legend()
    plt.title('Competitive RR vs MMR for {username}'.format(username=username))


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


def plot_elo_dm_games(username, games, weapon):
    elos = games[weapon]
    en = [i for i, d in enumerate(elos)]
    plt.figure()
    plt.plot(en, elos, 'b')
    tiers = [t for t in get_competitive_tiers() if "Un" not in t.get('tierName')]
    plt.yticks(list(get_tier_elo(t.get('tier'), global_elo_map) for t in tiers),
               list(t.get('tierName') for t in tiers))
    plt.grid(b=True, which='major', axis='y', color='#EEEEEE', linestyle='-')
    plt.xlabel('Matches')
    plt.ylabel('Elo')
    plt.title(f'Deathmatch Elo for {username} with {weapon}'.format(username=username, weapon=weapon))


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
            ra.append(round(median(running_avg), 2))
            # ra.append(round(sum(running_avg) / len(running_avg), 2))
        else:
            ra.append(None)

    dates = [g['date'] for g in games]
    en_dates = [i for i, d in enumerate(dates)]
    plt.figure()
    plt.scatter(dates, [g['kd'] for g in games], color='gray', label=f"kd")
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


@click.command()
@click.argument('username')  # In-game user
@click.option('--zone', default='eu', help="Valorant zone (eu, na etc)")
@click.option('--plot/--no-plot', default=True, help='Plot the result')
@click.option('--print/--no-print', 'print_', default=False, help='Print the games to terminal')
@click.option('--db-name', default=None, help="Database name and path. Default is ./{username}.db")
@click.option('--weapon', default=None, help="Show dm stats for this weapon only",
              type=click.Choice([d.get('displayName').lower() for d in get_all_weapons()]))
def valstats(username, zone, plot, print_, db_name, weapon):
    if db_name is None:
        db_name = username + '.db'
    weapon = weapon.title() if weapon else weapon
    name, tag = username.split('#')
    global auth
    auth = Auth(name, tag, zone)
    session = get_session(f"{username}.sqlitedb")
    user_id = get_user_id(session)
    if not user_id:
        return
    print("Loading database")
    if session.query(Match.id).count() == 0:
        matches = file_to_object(db_name) or {}
        for key, data in matches.items():
            session.add(Match(id=key, data=json.dumps(data)))
        session.commit()
    else:
        results = session.query(Match).all()
        matches = {res.id: json.loads(res.data) for res in results}

    new_matches = get_game_history(session, exclude=list(matches.keys()))
    if new_matches:
        for key, data in new_matches.items():
            session.add(Match(id=key, data=json.dumps(data)))
        session.commit()
        matches.update(new_matches)
    matches = sorted(matches.values(), key=lambda m: m.get('matchInfo').get('game_start'))
    matches = {m.get('matchInfo').get('matchid'): m for m in matches}

    # calibrate_elo(matches, global_elo_map)
    # return

    elo_dm_matches = process_dms_for_elo(matches, user_id)
    comp_matches = process_comp_matches(matches, user_id)
    dm_matches = process_dm_matches(auth, matches, user_id)
    if print_:
        print_comp_games(comp_matches)
        print_dm_games(dm_matches)
    if plot:
        plot_elo_dm_games(username, elo_dm_matches, weapon)
        # plot_dm_games(username, dm_matches, weapon, 'kd')
        plot_dm_games(username, dm_matches, weapon, 'performance')
        plot_comp_games(username, comp_matches)
        plt.show()


if __name__ == '__main__':
    valstats()
