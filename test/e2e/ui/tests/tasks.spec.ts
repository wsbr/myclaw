import { test, expect } from '@playwright/test';
import { login, gotoSidebar } from '../helpers';

test.describe('Tasks page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await gotoSidebar(page, 'Tasks');
  });

  test('renders task board with filter and table', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Tasks' })).toBeVisible();
    await expect(page.getByText('异步任务看板')).toBeVisible();
    await expect(page.getByRole('button', { name: /刷新/ })).toBeVisible();
    // Table headers
    await expect(page.getByText('状态')).toBeVisible();
  });
});
