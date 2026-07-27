import { test, expect } from '@playwright/test';
import { login, loadSeedData } from '../helpers';

test.describe('Session - Chat mode', () => {
  test('can send chat message and receive reply', async ({ page }) => {
    const { agentId } = loadSeedData();
    await login(page);
    await page.goto(`/agents/${agentId}/chat`);

    await expect(page.getByText('开始与')).toBeVisible({ timeout: 15_000 });
    await page.locator('input').last().fill('Hello from UI test');
    await page.getByRole('button', { name: '发送' }).click();

    // Wait for assistant reply
    await expect(page.getByText(/Mock assistant reply/)).toBeVisible({ timeout: 30_000 });
  });
});
