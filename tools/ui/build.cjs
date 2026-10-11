// Development-only asset build. No Node/npm or Tailwind compiler in production.
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const output = path.resolve(__dirname,'../../app/static/vendor');
fs.mkdirSync(output,{recursive:true});
let licenses = '';
for(const [pkg,file] of [['react','react.production.min.js'],['react-dom','react-dom.production.min.js']]) {
  const root=path.dirname(require.resolve(pkg+'/package.json'));
  fs.copyFileSync(path.join(root,'umd',file),path.join(output,file));
  licenses+='\n=== '+pkg+' ===\n'+fs.readFileSync(path.join(root,'LICENSE'),'utf8');
}
licenses+='\n=== tailwindcss ===\n'+fs.readFileSync(path.join(path.dirname(require.resolve('tailwindcss/package.json')),'LICENSE'),'utf8');
fs.writeFileSync(path.join(output,'LICENSES.txt'),licenses);
execFileSync(process.execPath,[require.resolve('tailwindcss/lib/cli.js'),'-i',path.join(__dirname,'input.css'),'-o',path.join(output,'tailwind.css'),'--content',path.resolve(__dirname,'../../app/static/index.html')+','+path.resolve(__dirname,'../../app/static/import-components.js')+','+path.resolve(__dirname,'../../app/static/scenario-components.js'),'--minify'],{stdio:'inherit'});
