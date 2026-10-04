"""Resolve unique provider records and preserve an explicit user choice."""
# Shared ambiguity handling keeps Books, Movies, and TV selections consistent.
import hashlib
import json


def matches_movie_year(item, year):
    return not item.get('year') or str(year or '')[:4] == str(item['year'])


def candidate_key(metadata):
    # Prefer the provider’s stable record key; hash sorted match data when no key is available.
    match = metadata.get('match', {})
    return match.get('key') or 'match:' + hashlib.sha256(json.dumps(match, sort_keys=True).encode()).hexdigest()


def resolve_candidates(candidates, item):
    # Deduplicate records and honor a saved choice; multiple remaining records require user selection.
    unique = {candidate_key(candidate): candidate for candidate in candidates}
    choice = item.get('match_choice', '')
    if choice and choice in unique:
        return unique[choice]
    values = list(unique.values())
    # Store the shared raw response once while retaining each candidate’s identifying metadata.
    if len(values) > 1:
        return {'response': values[0].get('response'),
                'candidates': [{key: value for key, value in candidate.items() if key != 'response'} for candidate in values]}
    return values[0] if values else None
