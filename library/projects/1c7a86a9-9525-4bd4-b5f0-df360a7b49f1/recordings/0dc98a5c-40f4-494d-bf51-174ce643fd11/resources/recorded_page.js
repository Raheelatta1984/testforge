const { locator, resolve } = require('./testforge_runtime.js');
class RecordedPage { constructor(page) { this.page=page; this.steps=require('./steps.json'); }
 locator(order,variables={}) { const step=this.steps.find(s=>s.order===order); if(!step) throw new Error('Unknown step'); return locator(this.page,resolve(step.selector,variables)); } }
module.exports={RecordedPage};
