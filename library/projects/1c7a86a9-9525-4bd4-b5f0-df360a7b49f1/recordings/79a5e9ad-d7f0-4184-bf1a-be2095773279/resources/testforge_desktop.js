// Portable deterministic Windows desktop simulator (export companion).
// Mirrors app/desktop_runner.py: same actions, same seeded Calculator flow.
// Live Windows runs go through the authenticated agent bridge (desktop_test.py
// / DesktopRunner.cs); this module never starts a process.
function calc(expression) {
  // Tiny recursive-descent arithmetic parser (digits and + - * / only); no eval.
  let i = 0; const s = String(expression);
  function expr() { let v = term(); while (s[i] === '+' || s[i] === '-') { const op = s[i++]; const r = term(); v = op === '+' ? v + r : v - r; } return v; }
  function term() { let v = factor(); while (s[i] === '*' || s[i] === '/') { const op = s[i++]; const r = factor(); v = op === '*' ? v * r : v / r; } return v; }
  function factor() { if (s[i] === '(') { i++; const v = expr(); if (s[i] !== ')') throw new Error('Bad expression'); i++; return v; } const m = /^(\d+(\.\d+)?)/.exec(s.slice(i)); if (!m) throw new Error('Bad expression'); i += m[0].length; return Number(m[0]); }
  const result = expr(); if (i !== s.length) throw new Error('Bad expression');
  return Number.isInteger(result) ? result : Math.round(result * 1e8) / 1e8;
}
const DESKTOP_ACTIONS = new Set(['window_attach','ui_click','ui_double_click','ui_right_click','ui_type','ui_select','ui_check','ui_menu_invoke','ui_grid_read','assert_control_text','assert_control_state','wait_window_ready']);
function control(automationId, name, text, controlType) { return {automation_id: automationId, name, text: String(text || ''), control_type: controlType || 'Edit', checked: false, state: 'enabled', selected: ''}; }
const CATALOG = {
  calculator: {window_title: 'Calculator', framework: 'dotnet', controls: [
    control('CalculatorResults', 'Display is 0', '0', 'Text'), control('num2Button', 'Two', '2', 'Button'),
    control('num3Button', 'Three', '3', 'Button'), control('plusButton', 'Plus', '+', 'Button'),
    control('equalButton', 'Equals', '=', 'Button'), control('clearButton', 'Clear', 'C', 'Button')]},
  custom_dotnet: {window_title: 'Custom .NET Application', framework: 'dotnet', controls: [
    control('txtName', 'Name', '', 'Edit'), control('btnSave', 'Save', 'Save', 'Button'),
    control('lblStatus', 'Status', 'Ready', 'Text'), control('gridOrders', 'Orders', 'Order 1001 | Ada | Open', 'DataGrid')]},
  custom_cpp: {window_title: 'Custom C++ Application', framework: 'cpp', controls: [
    control('1001', 'Name', '', 'Edit'), control('1002', 'Save', 'Save', 'Button'), control('1003', 'Status', 'Ready', 'Text')]},
  winforms_legacy: {window_title: 'Legacy WinForms Application', framework: 'winforms', controls: [
    control('txtCustomer', 'Customer', '', 'Edit'), control('chkActive', 'Active', 'Active', 'CheckBox'),
    control('cmbStatus', 'Status', 'Draft', 'ComboBox'), control('btnSubmit', 'Submit', 'Submit', 'Button'),
    control('lblResult', 'Result', 'Ready', 'Text'), control('gridCustomers', 'Customers', '1001 | Ada | Active', 'DataGrid')]},
};
class DesktopSimulator {
  constructor(appId) { this.catalog = JSON.parse(JSON.stringify(CATALOG)); this.app = null; if (appId && this.catalog[appId]) this.attach({window_title: this.catalog[appId].window_title}); }
  attach(selector, value) {
    const wanted = String(selector.window_title || value || '').toLowerCase();
    for (const app of Object.values(this.catalog)) {
      if (!wanted || app.window_title.toLowerCase().includes(wanted)) { this.app = JSON.parse(JSON.stringify(app)); return {window_title: app.window_title, ready: true}; }
    }
    throw new Error('No simulated window matches window_attach: ' + wanted);
  }
  find(selector) {
    if (!this.app) this.attach({window_title: 'Custom .NET Application'});
    for (const item of this.app.controls) {
      if (selector.automation_id && item.automation_id !== selector.automation_id) continue;
      if (selector.name && item.name !== selector.name) continue;
      if (selector.control_type && item.control_type !== selector.control_type) continue;
      return item;
    }
    const item = control(selector.automation_id || selector.name || 'control', selector.name || selector.automation_id || 'Control', '', selector.control_type);
    this.app.controls.push(item); return item;
  }
  perform(step, vars = {}) {
    const action = step.action, selector = step.selector || {}, value = step.value == null ? '' : String(step.value);
    if (!DESKTOP_ACTIONS.has(action)) throw new Error('Unsupported desktop action: ' + action);
    if (action === 'window_attach') return {action, ...this.attach(selector, value)};
    if (action === 'wait_window_ready') { if (!this.app) this.attach(selector, value); return {action, ready: true, window_title: this.app.window_title}; }
    const item = this.find(selector);
    switch (action) {
      case 'ui_click': case 'ui_double_click': case 'ui_right_click': {
        if (this.app.window_title === 'Calculator') {
          const display = this.app.controls.find(c => c.automation_id === 'CalculatorResults');
          if (item.automation_id === 'equalButton') {
            display.text = String(calc(display.text));
            display.name = 'Display is ' + display.text;
          } else if (item.automation_id === 'clearButton') display.text = '0';
          else if (item.text) display.text = (display.text === '0' ? '' : display.text) + item.text;
        }
        if (['Save', 'Submit', 'OK'].includes(item.name)) { const status = this.app.controls.find(c => c.control_type === 'Text'); if (status) status.text = 'Saved'; }
        return {action, control: item.name, text: item.text};
      }
      case 'ui_type': item.text = value; return {action, control: item.name, text: value};
      case 'ui_select': item.selected = value; item.text = value; return {action, control: item.name, selected: value};
      case 'ui_check': item.checked = !['false', '0', 'no', 'unchecked', 'off'].includes(value.toLowerCase()); item.state = item.checked ? 'checked' : 'unchecked'; return {action, control: item.name, checked: item.checked};
      case 'ui_menu_invoke': return {action, menu: value || selector.name || '', invoked: true};
      case 'ui_grid_read': vars[value || 'grid_text'] = item.text; return {action, control: item.name, captured: value || 'grid_text', text: item.text};
      case 'assert_control_text':
        if (item.text !== value) throw new Error(`assert_control_text: expected ${JSON.stringify(value)}, actual ${JSON.stringify(item.text)}`);
        return {assertion: action, expected: value, actual: item.text, matched: true};
      case 'assert_control_state': {
        const actual = ['checked', 'unchecked'].includes(value.toLowerCase()) ? (item.checked ? 'checked' : 'unchecked') : item.state;
        if (actual.toLowerCase() !== value.toLowerCase()) throw new Error(`assert_control_state: expected ${JSON.stringify(value)}, actual ${JSON.stringify(actual)}`);
        return {assertion: action, expected: value, actual, matched: true};
      }
      default: throw new Error('Unsupported desktop action: ' + action);
    }
  }
}
module.exports = {DesktopSimulator, DESKTOP_ACTIONS, CATALOG};
