import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu } from 'antd'
import {
  MessageOutlined,
  OrderedListOutlined,
  StarOutlined,
  AreaChartOutlined,
  DashboardOutlined,
  ThunderboltOutlined,
  CompassOutlined,
} from '@ant-design/icons'

const { Sider, Content } = Layout

const menuItems = [
  { key: '/', icon: <MessageOutlined />, label: '对话' },
  { key: '/picks', icon: <OrderedListOutlined />, label: '选股榜单' },
  { key: '/forecast', icon: <CompassOutlined />, label: '大盘预测' },
  { key: '/watchlist', icon: <StarOutlined />, label: '自选股' },
  { key: '/market', icon: <AreaChartOutlined />, label: '行情/概念' },
  { key: '/dashboard', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: '/execution', icon: <ThunderboltOutlined />, label: '执行看板' },
]

export default function MainLayout({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  // match path: "/" exact, and sub-paths like "/picks/123"
  const selectedKey = menuItems.find(
    (item) =>
      location.pathname === item.key ||
      (item.key !== '/' && location.pathname.startsWith(item.key)),
  )?.key || '/'

  return (
    <Layout style={{ height: '100vh' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        style={{ borderRight: '1px solid #303030' }}
      >
        <div
          style={{
            height: 48,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#fff',
            fontWeight: 700,
            fontSize: collapsed ? 14 : 18,
            flexDirection: 'column',
          }}
        >
          <span>{collapsed ? 'ZP' : 'Z-Plan'}</span>
          {!collapsed && <span style={{ fontSize: 9, color: '#888', fontWeight: 400 }}>build: {typeof __BUILD_TIME__ !== 'undefined' ? __BUILD_TIME__ : 'dev'}</span>}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Content
        style={{
          overflow: 'auto',
          height: '100vh',
          padding: 0,
        }}
      >
        {children}
      </Content>
    </Layout>
  )
}
