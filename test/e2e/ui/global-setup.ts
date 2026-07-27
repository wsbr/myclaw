/**
 * Seed test data via API before UI tests run.
 * Creates model provider + active agent, saves IDs for test specs.
 */
import { writeFileSync } from 'fs';
import { join } from 'path';

const API_URL = process.env.MYCLAW_FULLSTACK_API_URL || 'http://localhost:8001';
const MOCK_LLM_INTERNAL_URL = process.env.MYCLAW_MOCK_LLM_INTERNAL_URL || 'http://mock-llm:9090';
const ADMIN_EMAIL = process.env.INIT_ADMIN_EMAIL || 'admin@test.myclaw.local';
const ADMIN_PASSWORD = process.env.INIT_ADMIN_PASSWORD || 'testpassword123';

async function api(method: string, path: string, token?: string, body?: object) {
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${method} ${path} failed: ${res.status} ${text}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export default async function globalSetup() {
  // Wait for API
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`${API_URL}/health`);
      if (res.ok) break;
    } catch {
      // retry
    }
    await new Promise((r) => setTimeout(r, 2000));
  }

  const login = await api('POST', '/api/v1/auth/login', undefined, {
    email: ADMIN_EMAIL,
    password: ADMIN_PASSWORD,
  });
  const token = login.access_token;

  const provider = await api('POST', '/api/v1/model-providers', token, {
    name: `ui-test-provider-${Date.now()}`,
    provider: 'openai_compatible',
    base_url: `${MOCK_LLM_INTERNAL_URL}/v1`,
    api_key: 'sk-ui-test',
  });
  await api('PATCH', `/api/v1/model-providers/${provider.id}/activate`, token);

  const agent = await api('POST', '/api/v1/agents', token, {
    name: `UI Test Agent ${Date.now()}`,
    description: 'Agent for Playwright UI tests',
    modes: ['chat', 'plan'],
    model_config_: { provider_id: provider.id, model_id: 'mock-model' },
    system_prompt_blocks: { blocks: [{ type: 'text', content: 'You are helpful.' }] },
  });
  await api('POST', `/api/v1/agents/${agent.id}/activate`, token);

  const seedData = { agentId: agent.id, agentName: agent.name, token };
  writeFileSync(join(__dirname, '.seed-data.json'), JSON.stringify(seedData, null, 2));
  console.log(`Seeded agent: ${agent.id}`);
}
