import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Input, Card, List, Tag, Tabs, Typography } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import api from '../api/client'

const { Title } = Typography

export default function MarketPage() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [conceptSearch, setConceptSearch] = useState('')

  const { data: stocks } = useQuery({
    queryKey: ['market-stocks', search],
    queryFn: () => api.get('/market/stocks', { params: { q: search, limit: 30 } }),
    enabled: search.length >= 2,
  })

  const { data: concepts } = useQuery({
    queryKey: ['market-concepts', conceptSearch],
    queryFn: () => api.get('/market/concepts', { params: { q: conceptSearch, limit: 30 } }),
    enabled: conceptSearch.length >= 1,
  })

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Title level={4}>📈 行情 & 概念</Title>

      <Tabs
        defaultActiveKey="stocks"
        items={[
          {
            key: 'stocks',
            label: '股票搜索',
            children: (
              <Card>
                <Input
                  prefix={<SearchOutlined />}
                  placeholder="输入代码或名称搜索（至少2字符）"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  size="large"
                  style={{ marginBottom: 16 }}
                />
                <List
                  dataSource={stocks?.data?.stocks || []}
                  renderItem={(s: any) => (
                    <List.Item
                      style={{ cursor: 'pointer' }}
                      onClick={() => navigate(`/market/${s.ts_code}`)}
                    >
                      <List.Item.Meta
                        title={
                          <>
                            {s.name} <Tag>{s.ts_code}</Tag>
                          </>
                        }
                        description={`${s.industry || '-'} · ${s.market === 'a' ? 'A股' : '港股'}`}
                      />
                    </List.Item>
                  )}
                />
              </Card>
            ),
          },
          {
            key: 'concepts',
            label: '概念板块',
            children: (
              <Card>
                <Input
                  prefix={<SearchOutlined />}
                  placeholder="输入概念名称搜索"
                  value={conceptSearch}
                  onChange={(e) => setConceptSearch(e.target.value)}
                  size="large"
                  style={{ marginBottom: 16 }}
                />
                <List
                  dataSource={concepts?.data?.concepts || []}
                  renderItem={(c: any) => (
                    <List.Item style={{ cursor: 'pointer' }}>
                      <Tag color="purple" style={{ padding: '4px 12px', fontSize: 14 }}>
                        {c.name}
                      </Tag>
                    </List.Item>
                  )}
                />
              </Card>
            ),
          },
        ]}
      />
    </div>
  )
}
