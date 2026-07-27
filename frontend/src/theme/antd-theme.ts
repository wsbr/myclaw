import type { ThemeConfig } from 'antd'

/** Ant Design tokens aligned with liteClaw warm neutral palette */
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: '#000000',
    colorBgContainer: '#ffffff',
    colorBgLayout: '#faf9f6',
    colorBgElevated: '#ffffff',
    colorBorder: '#ebe8e0',
    colorBorderSecondary: '#f0ede6',
    borderRadius: 10,
    borderRadiusLG: 12,
    fontFamily:
      'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    colorText: '#1a1a1a',
    colorTextSecondary: '#737373',
    colorTextTertiary: '#a3a3a3',
    controlHeight: 36,
    controlHeightLG: 40,
  },
  components: {
    Layout: {
      siderBg: '#f5f3ef',
      headerBg: '#ffffff',
      bodyBg: '#faf9f6',
      triggerBg: '#ebe8e0',
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: '#ebe8e0',
      itemHoverBg: '#f0ede6',
      itemColor: '#525252',
      itemSelectedColor: '#1a1a1a',
      iconSize: 16,
    },
    Card: {
      borderRadiusLG: 12,
    },
    Table: {
      headerBg: '#f5f3ef',
      rowHoverBg: '#f9f8f5',
    },
    Tabs: {
      inkBarColor: '#000000',
      itemSelectedColor: '#1a1a1a',
      itemHoverColor: '#404040',
    },
  },
}
