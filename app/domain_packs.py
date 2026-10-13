"""Synthetic local examples only: no real accounts, payments or personal data.

Two families live here:

* the original five synthetic domain packs (commerce/banking/insurance/
  hrms/travel) served by ``/domain-demo.html`` — unchanged; and
* six enterprise ERP/CRM packs (Oracle, SAP Fiori, JD Edwards, Microsoft
  Dynamics 365 Finance & Operations, Microsoft Dynamics 365 Business
  Central, Odoo, Salesforce) served by the deterministic multi-tenant
  fixture ``/erp-crm-demo.html``.  That is six listed targets plus the
  Dynamics split, so the module exposes eleven pack ids in total.

Every enterprise pack carries a sign-in setup that only ever references
``TF_SECRET_*`` environment variables, reusable flows, a positive and a
negative dataset row, text/value/visibility assertions and a deterministic
reset teardown.  Nothing here talks to a real ERP/CRM system.
"""
from app.plan_runtime import normalize_plan

PACKS = {
    'commerce': ('E-commerce order', [{'amount': '2', 'expected': 'Total AUD 50.00'}, {'amount': '0', 'expected': 'Invalid quantity'}]),
    'banking': ('Bank transfer simulation', [{'amount': '100', 'expected': 'Balance AUD 900.00'}, {'amount': '1200', 'expected': 'Insufficient funds'}]),
    'insurance': ('Insurance quote simulation', [{'amount': '30', 'expected': 'Quote AUD 120.00'}, {'amount': '17', 'expected': 'Applicant must be 18 or older'}]),
    'hrms': ('Leave request simulation', [{'amount': '3', 'expected': 'Remaining leave 17 days'}, {'amount': '21', 'expected': 'Insufficient leave'}]),
    'travel': ('Travel booking simulation', [{'amount': '2', 'expected': 'Booking AUD 200.00'}, {'amount': '5', 'expected': 'Not enough seats'}]),
}


def _sel(primary, frames=None, **extra):
    selector = {'primary': primary}
    if frames:
        selector['frames'] = frames
    selector.update(extra)
    return selector


_ORACLE_FRAMES = ['#oracle-frame']


def _erp(name, input_sel, submit_sel, result_sel, records, negative,
         username_sel, signin_sel, shell_sel, flow_extra=(), setup_extra=(),
         extra_steps=(), password_sel=None, reset_sel=None):
    """Assemble one enterprise pack around the shared fixture contract."""
    positive_input, positive_expected = next(iter(records.items()))
    plan = {
        'variables': {
            'tenant': 'demo',
            'erp_user': {'env': 'TF_SECRET_ERP_USERNAME'},
            'erp_password': {'env': 'TF_SECRET_ERP_PASSWORD'},
        },
        'matrix': [{'browser': 'chromium', 'device': 'desktop', 'auth_profile': ''}],
        'execution_speed': 1.0,
        'setup': [
            {'action': 'navigate', 'value': '{{demo_url}}?pack=SLUG&tenant={{tenant}}'},
            {'action': 'fill', 'selector': username_sel, 'value': '{{erp_user}}'},
            {'action': 'fill', 'selector': password_sel or _sel('[name="password"]'), 'value': '{{erp_password}}'},
            {'action': 'click', 'selector': signin_sel},
            {'action': 'assert_visible', 'selector': shell_sel},
            *setup_extra,
        ],
        'flows': {
            'submit_record': [
                {'action': 'fill', 'selector': input_sel, 'value': '{{input}}'},
                *flow_extra,
                {'action': 'click', 'selector': submit_sel},
            ],
            'verify_record': [
                {'action': 'assert_text', 'selector': result_sel, 'value': '{{expected}}'},
                {'action': 'assert_value', 'selector': input_sel, 'value': '{{input}}'},
                {'action': 'assert_visible', 'selector': result_sel},
            ],
        },
        'steps': [{'flow': 'submit_record'}, {'flow': 'verify_record'}, *extra_steps],
        'dataset': [
            {'input': positive_input, 'expected': positive_expected},
            {'input': negative, 'expected': 'No record found: ' + negative},
        ],
        'teardown': [
            {'action': 'click', 'selector': reset_sel or _sel('#reset')},
            {'action': 'assert_text', 'selector': result_sel, 'value': 'Ready'},
            {'action': 'assert_value', 'selector': input_sel, 'value': ''},
        ],
    }
    return name, plan


# Selectors prioritise stable enterprise attributes (data-testid,
# data-sap-ui-id, data-field, name, aria-label, role) over generated ids, the
# same order the recorder now emits.
ERP_PACKS = {
    'oracle_web': _erp(
        'Oracle Fusion / EBS (web)',
        _sel('[data-testid="oracle-order-number"]', _ORACLE_FRAMES),
        _sel('[data-testid="oracle-submit"]', _ORACLE_FRAMES),
        _sel('[data-testid="oracle-result"]', _ORACLE_FRAMES),
        {'PO-1001': 'Purchase order PO-1001 approved for AUD 250.00'}, 'PO-9999',
        _sel('[data-testid="oracle-username"]', _ORACLE_FRAMES),
        _sel('[data-testid="oracle-sign-in"]', _ORACLE_FRAMES),
        _sel('[data-testid="oracle-shell"]', _ORACLE_FRAMES),
        password_sel=_sel('[data-testid="oracle-password"]', _ORACLE_FRAMES),
        reset_sel=_sel('[data-testid="oracle-reset"]', _ORACLE_FRAMES),
        flow_extra=[
            {'action': 'click', 'selector': _sel('[data-testid="oracle-lov-open"]', _ORACLE_FRAMES)},
            {'action': 'assert_visible', 'selector': _sel('[data-testid="oracle-lov-dialog"]', _ORACLE_FRAMES)},
            {'action': 'click', 'selector': _sel('[data-testid="oracle-lov-close"]', _ORACLE_FRAMES)},
        ],
        extra_steps=[
            {'action': 'assert_visible', 'selector': _sel('[data-testid="oracle-lines-table"]', ['#oracle-frame', '#oracle-lines-frame'])},
        ],
    ),
    'sap_fiori': _erp(
        'SAP Fiori / UI5',
        _sel('[data-sap-ui-id="salesOrderInput"]'),
        _sel('[data-sap-ui-id="salesOrderSubmit"]'),
        _sel('[data-testid="sap-result"]'),
        {'5001001': 'Sales order 5001001 confirmed for AUD 480.00'}, '5009999',
        _sel('[data-sap-ui-id="sapUsername"]'),
        _sel('[data-sap-ui-id="sapSignIn"]'),
        _sel('[data-testid="sap-shell"]'),
        extra_steps=[
            {'action': 'table_lookup',
             'selector': _sel('[data-testid="sap-grid"]', row_text='{{input}}', column='Status'),
             'value': 'order_status'},
        ],
    ),
    'jd_edwards': _erp(
        'JD Edwards EnterpriseOne',
        _sel('[data-testid="jde-order-number"]'),
        _sel('[data-testid="jde-find"]'),
        _sel('[data-testid="jde-batch-status"]'),
        {'1001': 'Batch 1001 status: Approved'}, '9999',
        _sel('[data-testid="jde-username"]'),
        _sel('[data-testid="jde-sign-in"]'),
        _sel('[data-testid="jde-shell"]'),
        setup_extra=[
            {'action': 'fill', 'selector': _sel('[data-testid="jde-fastpath"]'), 'value': 'P0411'},
            {'action': 'click', 'selector': _sel('[data-testid="jde-fastpath-go"]')},
            {'action': 'assert_text', 'selector': _sel('[data-testid="jde-heading"]'), 'value': 'Supplier Ledger Inquiry'},
        ],
    ),
    'ms_dynamics_fo': _erp(
        'Microsoft Dynamics 365 Finance & Operations',
        _sel('[data-testid="d365fo-sales-order"]'),
        _sel('[data-testid="d365fo-submit"]'),
        _sel('[data-testid="d365fo-result"]'),
        {'SO-1001': 'Sales order SO-1001 confirmed in Dynamics 365 F&O'}, 'SO-9999',
        _sel('[data-testid="d365fo-username"]'),
        _sel('[data-testid="d365fo-sign-in"]'),
        _sel('[data-testid="d365fo-shell"]'),
    ),
    'ms_dynamics_bc': _erp(
        'Microsoft Dynamics 365 Business Central',
        _sel('[data-testid="bc-sales-order"]'),
        _sel('[data-testid="bc-submit"]'),
        _sel('[data-testid="bc-result"]'),
        {'BC-1001': 'Sales order BC-1001 posted in Business Central'}, 'BC-9999',
        _sel('[data-testid="bc-username"]'),
        _sel('[data-testid="bc-sign-in"]'),
        _sel('[data-testid="bc-shell"]'),
    ),
    'odoo_erp': _erp(
        'Odoo ERP',
        _sel('[name="partner_id"]'),
        _sel('[data-testid="odoo-confirm"]'),
        _sel('[data-testid="odoo-result"]'),
        {'Acme Pty Ltd': 'Sales order SO-1001 confirmed for Acme Pty Ltd'}, 'Unknown Pty Ltd',
        _sel('[name="username"]'),
        _sel('[data-testid="odoo-sign-in"]'),
        _sel('[data-testid="odoo-shell"]'),
    ),
    'salesforce_crm': _erp(
        'Salesforce CRM (Lightning)',
        _sel('[data-testid="sf-opportunity"]'),
        _sel('[data-testid="sf-save"]'),
        _sel('[data-testid="sf-toast"]'),
        {'OPP-1001': 'Opportunity OPP-1001 saved at stage Qualification'}, 'OPP-9999',
        _sel('[data-testid="sf-username"]'),
        _sel('[data-testid="sf-sign-in"]'),
        _sel('[data-testid="sf-shell"]'),
    ),
}

# The Odoo input is a many2one autocomplete: typing must wait for and pick a
# suggestion instead of a plain fill.  Replace (not extend) its input step so
# the canonical submit flow exercises the wait-aware suggestion action.
_odoo_plan = ERP_PACKS['odoo_erp'][1]
_odoo_plan['flows']['submit_record'][0] = {
    'action': 'pick_suggestion', 'selector': _sel('[name="partner_id"]'), 'value': '{{input}}'}


def catalog():
    entries = [{'id': key, 'name': value[0], 'group': 'synthetic'} for key, value in PACKS.items()]
    entries += [{'id': key, 'name': value[0], 'group': 'erp_crm'} for key, value in ERP_PACKS.items()]
    return entries


def template(slug):
    if slug in PACKS:
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
                'can_save': True, 'diagnostics': [], 'demo_path': '/domain-demo.html',
                'notice': 'Synthetic local simulation. Set demo_url to this instance /domain-demo.html. No real transactions or domain certification.'}
    if slug in ERP_PACKS:
        name, authored = ERP_PACKS[slug]
        import copy
        plan = copy.deepcopy(authored)
        for stage in ('setup',):
            for step in plan[stage]:
                if isinstance(step.get('value'), str):
                    step['value'] = step['value'].replace('SLUG', slug)
        return {'name': name, 'execution_plan': plan, 'steps': normalize_plan(plan)['steps'],
                'can_save': True, 'diagnostics': [], 'demo_path': '/erp-crm-demo.html',
                'notice': ('Deterministic local ERP/CRM simulation on this instance '
                           '(/erp-crm-demo.html). Credentials are TF_SECRET_ERP_USERNAME / '
                           'TF_SECRET_ERP_PASSWORD environment references only; no real '
                           'enterprise system is contacted or certified.')}
    raise ValueError('Unknown synthetic pack')
