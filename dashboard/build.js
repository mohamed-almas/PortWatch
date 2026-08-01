const fs = require('fs');
const path = require('path');

const required = ['VITE_SUPABASE_URL', 'VITE_SUPABASE_ANON_KEY'];
for (const key of required) {
  if (!process.env[key]) {
    console.error(`Missing required env var: ${key}`);
    process.exit(1);
  }
}

let html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
html = html
  .replace('__VITE_SUPABASE_URL__', process.env.VITE_SUPABASE_URL)
  .replace('__VITE_SUPABASE_ANON_KEY__', process.env.VITE_SUPABASE_ANON_KEY);

fs.mkdirSync(path.join(__dirname, 'dist'), { recursive: true });
fs.writeFileSync(path.join(__dirname, 'dist', 'index.html'), html);
console.log('Built dist/index.html with injected env vars.');
