import { test, expect } from '@playwright/test';
import { login, loadSeedData } from '../helpers';

test.describe('Trace page', () => {
  test('can view trace after chat session', async ({ page }) => {
    const { agentId } = loadSeedData();
    await login(page);
    await page.goto(`/agents/${agentId}/chat`);

    // Create a chat interaction to generate trace data
    await page.locator('input').last().fill('Generate trace data');
    await page.getByRole('button', { name: '发送' }).click();
    await expect(page.getByText(/Mock assistant reply/)).toBeVisible({ timeout: 30_000 });

    // Open trace viewer
    await page.getByRole('button', { name: '查看 Trace' }).click();
    await expect(page).toHaveURL(/\/sessions\/.+\/trace/);

    // Trace page should show runner list
    await expect(page.getByText('Trace').or(page.getByText('llm_call'))).toBeVisible({ timeout: 15_000 });
  });
});
