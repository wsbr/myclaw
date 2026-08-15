import type { ThemeConfig } from 'antd'

/** Ant Design tokens aligned with MyClaw's warm enterprise palette. */
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: '#36499b',
    colorInfo: '#36499b',
    colorBgContainer: '#fffefa',
    colorBgLayout: '#faf8f2',
    colorBgElevated: '#fffefa',
    colorBorder: '#e7e3d9',
    colorBorderSecondary: '#efebe2',
    borderRadius: 12,
    borderRadiusLG: 16,
    fontFamily:
      'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    colorText: '#24283b',
    colorTextSecondary: '#696b78',
    colorTextTertiary: '#9999a2',
    controlHeight: 36,
    controlHeightLG: 40,
  },
  components: {
    Layout: {
      siderBg: '#f6f3ec',
      headerBg: '#fffefa',
      bodyBg: '#faf8f2',
      triggerBg: '#e8e5dc',
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: '#e8e9f5',
      itemHoverBg: '#eeece6',
      itemColor: '#666875',
      itemSelectedColor: '#31428d',
      iconSize: 16,
    },
    Card: {
      borderRadiusLG: 12,
    },
    Table: {
      headerBg: '#f5f2eb',
      rowHoverBg: '#f7f6fb',
    },
    Tabs: {
      inkBarColor: '#36499b',
      itemSelectedColor: '#31428d',
      itemHoverColor: '#4a5daf',
    },
  },
}
