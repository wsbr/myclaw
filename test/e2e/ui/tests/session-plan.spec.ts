import { test, expect } from '@playwright/test';
import { login, loadSeedData } from '../helpers';

test.describe('Session - Plan mode', () => {
  test('can generate and execute plan end-to-end', async ({ page }) => {
    const { agentId } = loadSeedData();
    await login(page);
    await page.goto(`/agents/${agentId}/chat`);

    await expect(page.getByText('开始与')).toBeVisible({ timeout: 15_000 });

    // Generate plan
    await page.locator('input').last().fill('Analyze Q3 sales performance');
    await page.getByRole('button', { name: '生成计划' }).click();

    // Wait for plan card
    await expect(page.getByText('执行计划：')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('Collect sales data from Q3')).toBeVisible();

    // Execute plan
    await page.getByRole('button', { name: '确认执行' }).click();
    await expect(page.getByText('计划已开始执行')).toBeVisible();

    // Wait for completion (WebSocket push)
    await expect(page.getByText('计划执行完成')).toBeVisible({ timeout: 120_000 });

    // Verify step outputs appear
    await expect(page.getByText(/Completed output for s1/)).toBeVisible();
  });
});
