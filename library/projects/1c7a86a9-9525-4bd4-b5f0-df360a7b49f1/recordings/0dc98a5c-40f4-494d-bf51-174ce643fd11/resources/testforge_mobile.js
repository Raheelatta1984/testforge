// Portable deterministic mobile/Zebra simulator (export companion).
// Mirrors app/mobile_runner.py. Live Appium/DataWedge runs go through the
// authenticated hub bridge (mobile_test.py); this module contacts nothing.
const MOBILE_ACTIONS = new Set(['app_launch','tap_element','long_press','swipe','mobile_type','device_key','rotate_orientation','toggle_network_mode','scan_barcode','assert_mobile_text','assert_mobile_visible']);
const PRODUCTS = {'4006381333931': 'TestForge Scanner Demo Item', 'TF-QR-001': 'TestForge QR Demo Item', '1234567890128': 'Warehouse Demo Carton'};
function ean13Ok(data) {
  if (!/^\d{13}$/.test(data || '')) return false;
  let total = 0; for (let i = 0; i < 12; i++) total += Number(data[i]) * (i % 2 ? 3 : 1);
  return (10 - (total % 10)) % 10 === Number(data[12]);
}
function validateBarcode(selector, value) {
  const symbology = String(selector.symbology || 'CODE-128').toUpperCase();
  if (!['EAN-13', 'CODE-128', 'QR', 'GS1-128'].includes(symbology)) throw new Error('Unsupported symbology: ' + symbology);
  const data = String(selector.barcode_data || value || '');
  if (!data || data.length > 256) throw new Error('scan_barcode needs barcode_data');
  if (symbology === 'EAN-13' && !ean13Ok(data)) throw new Error('EAN-13 barcode_data must be 13 digits with a valid check digit');
  if (symbology === 'GS1-128' && !(data.startsWith('(01)') || data.startsWith('01'))) throw new Error('GS1-128 barcode_data must start with (01)');
  return {symbology, data};
}
function datawedgePayload(symbology, barcodeData, profileName = 'TestForge') {
  return {platform: 'zebra', intent: {action: 'android.intent.action.MAIN', broadcast: true,
    datawedge_api: 'com.symbol.datawedge.api.SOFTWARE_TRIGGER',
    extras: {'com.symbol.datawedge.profile': profileName, 'com.symbol.datawedge.data_string': barcodeData, 'com.symbol.datawedge.label_type': symbology}},
    symbology, barcode_data: barcodeData};
}
function datawedgeProfile(profileName = 'TestForge') {
  return {profile_name: profileName, profile_enabled: true,
    intent_output: {enabled: true, intent_action: 'com.testforge.DATAWEDGE_SCAN', intent_delivery: 'broadcast'},
    barcode_input: {enabled: true, symbologies: ['CODE-128', 'EAN-13', 'GS1-128', 'QR']},
    keystroke_output: {enabled: false}};
}
class MobileSimulator {
  constructor(platform = 'android') {
    if (!['android', 'ios', 'zebra'].includes(platform)) throw new Error('platform must be android, ios or zebra');
    this.platform = platform; this.orientation = 'portrait'; this.network = 'online'; this.app = null;
    this.elements = {home_title: {text: platform === 'zebra' ? 'Warehouse Home' : 'Home', visible: true},
      scan_result: {text: '', visible: true}, product_name: {text: '', visible: true},
      quantity: {text: '1', visible: true}, status: {text: 'Ready', visible: true}};
  }
  find(selector) {
    for (const field of ['accessibility_id', 'resource_id', 'text', 'ios_predicate', 'ios_class_chain', 'xpath']) {
      if (selector[field] && this.elements[selector[field]]) return this.elements[selector[field]];
    }
    const key = Object.keys(selector).sort().map(k => selector[k]).join('|') || 'element';
    if (!this.elements[key]) this.elements[key] = {text: selector.text || '', visible: true};
    return this.elements[key];
  }
  perform(step, vars = {}) {
    const action = step.action, selector = step.selector || {}, value = step.value == null ? '' : String(step.value);
    if (!MOBILE_ACTIONS.has(action)) throw new Error('Unsupported mobile action: ' + action);
    switch (action) {
      case 'app_launch': this.app = value || selector.app_id || selector.bundle_id || 'demo.app'; return {action, app: this.app, platform: this.platform, launched: true};
      case 'tap_element': case 'long_press': { const el = this.find(selector); return {action, text: el.text, long_press: action === 'long_press'}; }
      case 'swipe': { const direction = (value || selector.direction || 'up').toLowerCase(); if (!['up', 'down', 'left', 'right'].includes(direction)) throw new Error('swipe value must be up, down, left or right'); return {action, direction}; }
      case 'mobile_type': { const el = this.find(selector); el.text = value; return {action, text: value}; }
      case 'device_key': { const key = value || selector.key || ''; if (!['Enter', 'Tab', 'Escape', 'Backspace', 'Scan', 'SCAN_TRIGGER'].includes(key) && !/^F([1-9]|1[0-2])$/.test(key)) throw new Error('device_key must be Enter, Tab, F1-F12, Escape, Backspace or Scan'); return {action, key}; }
      case 'rotate_orientation': { const wanted = (value || selector.orientation || '').toLowerCase(); if (!['portrait', 'landscape'].includes(wanted)) throw new Error('rotate_orientation value must be portrait or landscape'); this.orientation = wanted; return {action, orientation: wanted}; }
      case 'toggle_network_mode': { const mode = (value || selector.mode || '').toLowerCase(); if (!['online', 'offline', 'wifi_on', 'wifi_off', 'airplane_on', 'airplane_off', 'data_on', 'data_off'].includes(mode)) throw new Error('Unsupported network mode'); this.network = ['offline', 'wifi_off', 'airplane_on', 'data_off'].includes(mode) ? 'offline' : 'online'; return {action, mode, network: this.network}; }
      case 'scan_barcode': {
        const {symbology, data} = validateBarcode(selector, value);
        const product = PRODUCTS[data] || null;
        this.elements.scan_result.text = symbology + ':' + data;
        this.elements.product_name.text = product || '';
        this.elements.status.text = product ? 'Item found: ' + product : 'Scanned ' + symbology + ' code';
        return {action, symbology, barcode_data: data, product, scan_result: this.elements.scan_result.text,
          datawedge: this.platform === 'zebra' ? datawedgePayload(symbology, data) : null};
      }
      case 'assert_mobile_text': { const el = this.find(selector); if (el.text !== value) throw new Error(`assert_mobile_text: expected ${JSON.stringify(value)}, actual ${JSON.stringify(el.text)}`); return {assertion: action, expected: value, actual: el.text, matched: true}; }
      case 'assert_mobile_visible': { const el = this.find(selector); if (!el.visible) throw new Error('assert_mobile_visible: expected "visible", actual "hidden"'); return {assertion: action, expected: 'visible', actual: 'visible', matched: true}; }
      default: throw new Error('Unsupported mobile action: ' + action);
    }
  }
}
module.exports = {MobileSimulator, MOBILE_ACTIONS, datawedgePayload, datawedgeProfile, validateBarcode};
