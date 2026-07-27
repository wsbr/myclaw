import { test, expect } from '@playwright/test';
import { login, gotoSidebar } from '../helpers';

const PAGES = [
  { label: 'Agents', heading: 'Agents' },
  { label: 'Tasks', heading: 'Tasks' },
  { label: '用户管理', heading: '用户管理' },
  { label: 'Tool 管理', heading: 'Tool 管理' },
  { label: 'Skill 管理', heading: 'Skill 管理' },
  { label: 'MCP 管理', heading: 'MCP 管理' },
  { label: '模型管理', heading: '模型 Provider' },
];

test.describe('Sidebar navigation', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  for (const { label, heading } of PAGES) {
    test(`can navigate to ${label}`, async ({ page }) => {
      await gotoSidebar(page, label);
      await expect(page.getByRole('heading', { name: heading })).toBeVisible();
    });
  }
});
