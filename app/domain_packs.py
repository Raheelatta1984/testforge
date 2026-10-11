"""Synthetic local examples only: no real accounts, payments or personal data."""
from app.plan_runtime import normalize_plan

PACKS = {
    'commerce': ('E-commerce order', [{'amount': '2', 'expected': 'Total AUD 50.00'}, {'amount': '0', 'expected': 'Invalid quantity'}]),
    'banking': ('Bank transfer simulation', [{'amount': '100', 'expected': 'Balance AUD 900.00'}, {'amount': '1200', 'expected': 'Insufficient funds'}]),
    'insurance': ('Insurance quote simulation', [{'amount': '30', 'expected': 'Quote AUD 120.00'}, {'amount': '17', 'expected': 'Applicant must be 18 or older'}]),
    'hrms': ('Leave request simulation', [{'amount': '3', 'expected': 'Remaining leave 17 days'}, {'amount': '21', 'expected': 'Insufficient leave'}]),
    'travel': ('Travel booking simulation', [{'amount': '2', 'expected': 'Booking AUD 200.00'}, {'amount': '5', 'expected': 'Not enough seats'}]),
}


def template(slug):
    if slug not in PACKS:
        raise ValueError('Unknown synthetic pack')
    name, dataset = PACKS[slug]
    plan = {
        'setup': [{'action': 'navigate', 'value': '{{demo_url}}?pack=' + slug}],
        'flows': {'submit': [
            {'action': 'fill', 'selector': {'primary': '#amount'}, 'value': '{{amount}}'},
            {'action': 'click', 'selector': {'primary': '#submit'}}]},
        'steps': [{'flow': 'submit'}, {'action': 'assert_text', 'selector': {'primary': '#result'}, 'value': '{{expected}}'}],
        'dataset': dataset,
        'teardown': [{'action': 'click', 'selector': {'primary': '#reset'}},
                     {'action': 'assert_text', 'selector': {'primary': '#result'}, 'value': 'Ready'}],
    }
    return {'name': name, 'execution_plan': plan, 'steps': normalize_plan(plan)['steps'],
            'can_save': True, 'diagnostics': [], 'notice': 'Synthetic local simulation. Set demo_url to this instance /domain-demo.html. No real transactions or domain certification.'}
