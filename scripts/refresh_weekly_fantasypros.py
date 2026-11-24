"""Read current FantasyPros data into a private snapshot; never change a roster or DB.

Official request schema: https://api.fantasypros.com/public/v2/docs
Credentials are loaded by the existing secret-safe helper and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from check_fantasypros_api import LOCAL_ENV, ROOT, load_local_env
from fantasypros_http import fantasypros_request


def validate_payload(name: str, body: dict, season: int, week: int) -> list[str]:
    """Fail closed on the actual wrong-season and wrong-ranking-type responses."""
    errors = []
    if name.startswith('weekly_') or name in ('ros_projections', 'actual_ppr_points'):
        if str(body.get('season', body.get('year'))) != str(season):
            errors.append('Missing or mismatched returned season')
        if not isinstance(body.get('players'), list):
            errors.append('Missing player collection')
    if name.startswith('weekly_') or name == 'ros_projections':
        if str(body.get('week')) != str(week):
            errors.append('Missing or mismatched returned week')
        if not body.get('players'):
            errors.append('Requested forecast collection is empty')
    if name.startswith('weekly_ecr'):
        if body.get('ranking_type_name') != 'weekly':
            errors.append('Returned rankings are not weekly start/sit rankings')
        if body.get('scoring') != 'PPR':
            errors.append('Returned ranking scoring is not PPR')
    if name == 'ros_projections' and body.get('ros_projections') is not True:
        errors.append('Response does not confirm rest-of-season projections')
    if name == 'actual_ppr_points' and isinstance(body.get('players'), list):
        if any(set(str(k) for k in p.get('weeks', {})) - {str(week)} for p in body['players']):
            errors.append('Actual points include weeks outside the requested week')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--season', type=int, required=True)
    parser.add_argument('--week', type=int, required=True, choices=range(1, 19))
    parser.add_argument('--include-ros', action='store_true')
    parser.add_argument('--include-results', action='store_true')
    parser.add_argument('--results-week', type=int, choices=range(1, 19), help='Last completed week for results; defaults to --week')
    args = parser.parse_args()
    load_local_env(LOCAL_ENV)
    key = os.environ.get('FANTASYPROS_API_KEY')
    if not key:
        print(json.dumps({'ok': False, 'error': 'FANTASYPROS_API_KEY is not configured'}))
        return 2
    now = datetime.now(timezone.utc)
    output = ROOT / '.local' / 'season-management' / 'api' / now.strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True)
    base = f'https://api.fantasypros.com/public/v2/json/nfl'
    # WEEKLY is verified live. The published NFL enum omits it; WW means waiver wire.
    datasets = [(f'weekly_ecr_{pos.lower()}', f'{args.season}/consensus-rankings',
                 {'position': pos, 'scoring': 'PPR', 'type': 'WEEKLY', 'week': args.week})
                for pos in ('FLX', 'QB', 'K', 'DST')]
    datasets += [
        ('weekly_projections', f'{args.season}/projections', {'position': 'ALL', 'week': args.week}),
        ('injuries', 'injuries', {'year': args.season, 'week': args.week, 'include_probabilities': 'true'}),
        ('news', 'news', {'limit': 100, 'order_by': 'updated'}),
    ]
    if args.include_ros:
        datasets.append(('ros_projections', f'{args.season}/projections', {'position': 'ALL', 'week': args.week, 'ros': 'true'}))
    if args.include_results:
        results_week = args.results_week or args.week
        datasets.append(('actual_ppr_points', f'{args.season}/player-points', {'start': results_week, 'end': results_week, 'position': 'ALL', 'scoring': 'PPR'}))
    checks = []
    for index, (name, path, params) in enumerate(datasets):
        if index:
            time.sleep(1.1)
        url = f'{base}/{path}?{urlencode(params)}'
        check = {'dataset': name, 'url': url, 'retrieved_at': datetime.now(timezone.utc).isoformat()}
        request = fantasypros_request(url, key, user_agent='fantasy-gm-personal/0.1')
        try:
            with urlopen(request, timeout=30) as response:
                body = json.load(response)
                check.update(status=response.status, response_date=response.headers.get('Date'), cache_age=response.headers.get('Age'))
            (output / f'{name}.json').write_text(json.dumps(body, indent=2), encoding='utf-8')
            check['ok'] = True
            if isinstance(body, dict):
                check['metadata'] = {k: v for k, v in body.items() if not isinstance(v, (list, dict))}
                check['collections'] = {k: len(v) for k, v in body.items() if isinstance(v, (list, dict))}
                expected_week = (args.results_week or args.week) if name == 'actual_ppr_points' else args.week
                errors = validate_payload(name, body, args.season, expected_week)
                if errors:
                    check.update(ok=False, errors=errors)
                check['season_week_echoed'] = ('season' in body or 'year' in body) and 'week' in body
                if body.get('public_api_limited'):
                    check['coverage_warning'] = 'Provider marks public_api_limited; do not assume unrestricted coverage.'
            elif isinstance(body, list):
                check['record_count'] = len(body)
            else:
                check.update(ok=False, error='Unexpected JSON shape')
        except HTTPError as exc:
            check.update(ok=False, status=exc.code)
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            check.update(ok=False, error=type(exc).__name__)
        checks.append(check)
    result = {'ok': all(c['ok'] for c in checks), 'requested_season': args.season, 'requested_week': args.week, 'results_week': (args.results_week or args.week) if args.include_results else None, 'snapshot_dir': str(output), 'checks': checks,
              'interpretation': 'HTTP success is access, not proof of completeness or freshness. Inspect returned metadata and player coverage; empty actuals before games are not zero-point outcomes. PPR points are not ESPN custom league points.'}
    (output / 'manifest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
