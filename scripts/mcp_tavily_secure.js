import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const secretsHome = process.env.MORNING_DISPATCH_SECRETS_DIR || path.join(process.env.HOME, '.morning-dispatch/secrets');
const keyPath = path.join(secretsHome, 'tavily/api_key');

let apiKey = process.env.TAVILY_API_KEY;
if (!apiKey && fs.existsSync(keyPath)) {
  apiKey = fs.readFileSync(keyPath, 'utf8').trim();
}

if (!apiKey) {
  console.error("TAVILY_API_KEY is not configured.");
  process.exit(1);
}

// Inject arguments programmatically to avoid command line exposure in `ps`
process.argv = [
  process.argv[0],
  fileURLToPath(import.meta.url),
  'https://mcp.tavily.com/mcp/',
  '--header',
  `Authorization: Bearer ${apiKey}`
];

// Dynamically import the ESM proxy entry point from the installed package
await import('../node_modules/mcp-remote/dist/proxy.js');
