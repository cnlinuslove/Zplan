import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Input, Card, List, Tag, Tabs, Typography, Spin, Empty, Button, Modal, Table } from 'antd'
import { SearchOutlined, FireOutlined } from '@ant-design/icons'
import api from '../api/client'

const { Title } = Typography

export default function MarketPage() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [conceptSearch, setConceptSearch] = useState('')
  const [selectedConcept, setSelectedConcept] = useState<string | null>(null)

  // 股票搜索：至少 1 个字符触发
  const { data: stocks, isFetching: stocksLoading } = useQuery({
    queryKey: ['market-stocks', search],
    queryFn: () => api.get('/market/stocks', { params: { q: search, limit: 30 } }),
    enabled: search.length >= 1,
  })

  // 热门概念（页面加载时自动获取）
  const { data: hotConcepts } = useQuery({
    queryKey: ['market-concepts', ''],
    queryFn: () => api.get('/market/concepts', { params: { limit: 50 } }),
  })

  // 搜索概念
  const { data: searchConcepts, isFetching: conceptsLoading } = useQuery({
    queryKey: ['market-concepts', conceptSearch],
    queryFn: () => api.get('/market/concepts', { params: { q: conceptSearch, limit: 50 } }),
    enabled: conceptSearch.length >= 1,
  })

  const displayConcepts = conceptSearch
    ? (searchConcepts?.data?.concepts || [])
    : (hotConcepts?.data?.concepts || [])

  // 点击概念后加载成份股
  const { data: conceptStocks, isFetching: conceptStocksLoading } = useQuery({
    queryKey: ['concept-stocks', selectedConcept],
    queryFn: () => api.get(`/market/concepts/${encodeURIComponent(selectedConcept!)}/stocks`),
    enabled: !!selectedConcept,
  })

  const stockList = stocks?.data?.stocks || []

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
                  placeholder="输入代码或名称搜索（如：平安、600519）"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  size="large"
                  style={{ marginBottom: 16 }}
                  allowClear
                />
                {stocksLoading ? (
                  <Spin style={{ display: 'block', margin: '40px auto' }} />
                ) : search && stockList.length === 0 ? (
                  <Empty description="未找到匹配股票" />
                ) : (
                  <List
                    dataSource={stockList}
                    renderItem={(s: any) => (
                      <List.Item
                        style={{ cursor: 'pointer' }}
                        onClick={() => navigate(`/market/${s.ts_code}`)}
                      >
                        <List.Item.Meta
                          title={
                            <span>
                              <strong>{s.name}</strong>{' '}
                              <Tag>{s.ts_code}</Tag>
                              <Tag color="blue">{s.market === 'a' ? 'A股' : '港股'}</Tag>
                            </span>
                          }
                          description={s.industry || '-'}
                        />
                        <Button size="small">查看 →</Button>
                      </List.Item>
                    )}
                  />
                )}
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
                  placeholder="搜索概念（如：AI、芯片、新能源）"
                  value={conceptSearch}
                  onChange={(e) => setConceptSearch(e.target.value)}
                  size="large"
                  style={{ marginBottom: 16 }}
                  allowClear
                />
                {!conceptSearch && (
                  <div style={{ marginBottom: 12, color: '#888', fontSize: 13 }}>
                    <FireOutlined style={{ color: '#ff4d4f', marginRight: 4 }} />
                    热门概念（按成份股数量排序），点击可查看
                  </div>
                )}
                {conceptsLoading ? (
                  <Spin style={{ display: 'block', margin: '20px auto' }} />
                ) : displayConcepts.length === 0 ? (
                  <Empty description={conceptSearch ? '未找到匹配概念' : '暂无概念数据'} />
                ) : (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    {displayConcepts.map((c: any) => (
                      <Tag
                        key={c.name}
                        color="purple"
                        style={{
                          padding: '6px 14px',
                          fontSize: 14,
                          cursor: 'pointer',
                          borderRadius: 16,
                        }}
                        onClick={() => setSelectedConcept(c.name)}
                      >
                        {c.name}
                        {c.stock_count != null && (
                          <span style={{ marginLeft: 4, opacity: 0.7, fontSize: 11 }}>
                            ({c.stock_count})
                          </span>
                        )}
                      </Tag>
                    ))}
                  </div>
                )}
              </Card>
            ),
          },
        ]}
      />

      {/* 概念成份股弹窗 */}
      <Modal
        title={`🏷️ ${selectedConcept || ''} — 成份股`}
        open={!!selectedConcept}
        onCancel={() => setSelectedConcept(null)}
        footer={null}
        width={600}
      >
        {conceptStocksLoading ? (
          <Spin style={{ display: 'block', margin: '40px auto' }} />
        ) : (
          <Table
            dataSource={conceptStocks?.data?.stocks || []}
            rowKey="ts_code"
            size="small"
            pagination={false}
            columns={[
              { title: '代码', dataIndex: 'ts_code', key: 'ts_code', width: 100 },
              { title: '名称', dataIndex: 'name', key: 'name', width: 120 },
            ]}
            onRow={(record: any) => ({
              onClick: () => { setSelectedConcept(null); navigate(`/market/${record.ts_code}`) },
              style: { cursor: 'pointer' },
            })}
          />
        )}
      </Modal>
    </div>
  )
}
