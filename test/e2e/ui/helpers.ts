import { readFileSync } from 'fs';
import { join } from 'path';
import { Page } from '@playwright/test';

export interface SeedData {
  agentId: string;
  agentName: string;
  token: string;
}

export function loadSeedData(): SeedData {
  const raw = readFileSync(join(__dirname, '.seed-data.json'), 'utf-8');
  return JSON.parse(raw);
}

export async function login(page: Page) {
  await page.goto('/login');
  await page.getByPlaceholder('admin@myclaw.local').fill('admin@test.myclaw.local');
  await page.locator('input[type="password"]').fill('testpassword123');
  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForURL(/\/(agents)?$/);
}

export async function gotoSidebar(page: Page, label: string) {
  await page.getByRole('button', { name: label }).click();
}
