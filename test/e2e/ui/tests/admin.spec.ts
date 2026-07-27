import { test, expect } from '@playwright/test';
import { login, gotoSidebar } from '../helpers';

test.describe('Admin pages', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('users page shows admin and supports create modal', async ({ page }) => {
    await gotoSidebar(page, '用户管理');
    await expect(page.getByRole('heading', { name: '用户管理' })).toBeVisible();
    await expect(page.getByText('admin@test.myclaw.local')).toBeVisible();
    await page.getByRole('button', { name: /新建/ }).click();
    await expect(page.getByText('新建用户')).toBeVisible();
  });

  test('tools page lists builtin tools', async ({ page }) => {
    await gotoSidebar(page, 'Tool 管理');
    await expect(page.getByRole('heading', { name: 'Tool 管理' })).toBeVisible();
    // Wait for table to load
    await page.waitForSelector('.ant-table', { timeout: 10_000 });
    await expect(page.locator('.ant-table-tbody tr').first()).toBeVisible();
  });

  test('skills page loads', async ({ page }) => {
    await gotoSidebar(page, 'Skill 管理');
    await expect(page.getByRole('heading', { name: 'Skill 管理' })).toBeVisible();
  });

  test('mcp page loads', async ({ page }) => {
    await gotoSidebar(page, 'MCP 管理');
    await expect(page.getByRole('heading', { name: 'MCP 管理' })).toBeVisible();
  });

  test('models page loads', async ({ page }) => {
    await gotoSidebar(page, '模型管理');
    await expect(page.getByRole('heading', { name: '模型 Provider' })).toBeVisible();
  });
});
