import { test, expect } from '@playwright/test';
import { login, loadSeedData } from '../helpers';

test.describe('Agents page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('shows seeded agent and can open config', async ({ page }) => {
    const { agentName } = loadSeedData();
    await expect(page.getByText(agentName)).toBeVisible();

    const card = page.locator('.group').filter({ hasText: agentName });
    await card.getByTitle('配置').click();

    await expect(page).toHaveURL(/\/agents\/.+\/config/);
  });

  test('can navigate to new agent config', async ({ page }) => {
    await page.getByRole('button', { name: '新建 Agent' }).click();
    await expect(page).toHaveURL(/\/agents\/new\/config/);
  });
});
