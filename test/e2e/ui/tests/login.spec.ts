import { test, expect } from '@playwright/test';
import { login } from '../helpers';

test.describe('Login', () => {
  test('shows login page and rejects bad credentials', async ({ page }) => {
    await page.goto('/login');
    await expect(page.getByText('MyClaw')).toBeVisible();
    await expect(page.getByText('登录')).toBeVisible();

    await page.getByPlaceholder('admin@myclaw.local').fill('wrong@test.myclaw.local');
    await page.locator('input[type="password"]').fill('wrong');
    await page.getByRole('button', { name: '登录' }).click();
    await expect(page.getByText('邮箱或密码错误')).toBeVisible();
  });

  test('admin can login and reach agents page', async ({ page }) => {
    await login(page);
    await expect(page.getByText('Agents')).toBeVisible();
    await expect(page.getByRole('button', { name: '新建 Agent' })).toBeVisible();
  });
});
