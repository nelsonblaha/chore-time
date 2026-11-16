"""Room tidiness, independent of robot cleaning history and reward points."""


def space_states(spaces, board, active, paused, events, now):
    result = {}
    for space in spaces:
        key = space['id']
        jobs = space.get('chores', [])
        row = {k: space[k] for k in ('id', 'name', 'floor', 'map_rooms', 'chores') if k in space}
        row.update(state='unknown', last_tidy=0, tidy_until=0)
        if jobs:
            rows = [board.get(job, {}) for job in jobs]
            if any(a.get('chore') in jobs for a in active.values()):
                row['state'] = 'doing'
            elif any(r.get('pending_by') for r in rows):
                row['state'] = 'waiting'
            elif any(a.get('chore') in jobs for a in paused.values()):
                row['state'] = 'paused'
            elif all(job in board for job in jobs):
                row['last_tidy'] = min(r.get('last_clean', 0) for r in rows)
                row['tidy_until'] = min(r.get('clean_until', 0) for r in rows)
                row['state'] = 'tidy' if row['tidy_until'] > now else 'untidy'
        else:
            latest = next((e for e in reversed(events) if e.get('type') == 'space_tidy' and e.get('space') == key), None)
            if latest:
                row['by'] = latest.get('by')
                row['last_tidy'] = latest['ts'] if latest.get('tidy') else 0
                row['tidy_until'] = latest['ts'] + float(space.get('cooldown_h', 72)) * 3600 if latest.get('tidy') else 0
                row['state'] = 'tidy' if row['tidy_until'] > now else 'untidy'
        result[key] = row
    return result
