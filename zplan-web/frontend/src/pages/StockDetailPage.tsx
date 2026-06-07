import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, Descriptions, Tag, Button, Spin, Typography, Space, List } from 'antd'
import { ArrowLeftOutlined, StarOutlined, StarFilled } from '@ant-design/icons'
import { useState } from 'react'
import api from '../api/client'

const { Title } = Typography

export default function StockDetailPage() {
  const { tsCode } = useParams()
  const navigate = useNavigate()
  const [watching, setWatching] = useState(false)

  const { data: detail, isLoading } = useQuery({
    queryKey: ['stock-detail', tsCode],
    queryFn: () => api.get(`/market/stocks/${tsCode}/detail`),
    enabled: !!tsCode,
  })

  const { data: bars } = useQuery({
    queryKey: ['stock-bars', tsCode],
    queryFn: () => api.get(`/market/stocks/${tsCode}/bars`, { params: { days: 60 } }),
    enabled: !!tsCode,
  })

  const { data: news } = useQuery({
    queryKey: ['stock-news', tsCode],
    queryFn: () => api.get(`/market/stocks/${tsCode}/news`, { params: { limit: 10 } }),
    enabled: !!tsCode,
  })

  if (isLoading) return <Spin style={{ display: 'block', margin: '40vh auto' }} />
  const s = detail?.data?.stock
  if (!s) return <div style={{ padding: 24 }}>未找到 {tsCode}</div>

  const latest = s.latest

  async function toggleWatchlist() {
    if (watching) {
      await api.delete(`/watchlist/${tsCode}`)
      setWatching(false)
    } else {
      await api.post('/watchlist', { ts_code: tsCode })
      setWatching(true)
    }
  }

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>返回</Button>
        <Button
          icon={watching ? <StarFilled /> : <StarOutlined />}
          onClick={toggleWatchlist}
          type={watching ? 'primary' : 'default'}
        >
          {watching ? '已关注' : '加入自选'}
        </Button>
      </Space>

      <Card style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          {s.name} ({s.ts_code})
          <Tag style={{ marginLeft: 8 }}>{s.market === 'a' ? 'A股' : '港股'}</Tag>
        </Title>
        <Descriptions bordered size="small" column={4} style={{ marginTop: 12 }}>
          <Descriptions.Item label="行业">{s.industry || '-'}</Descriptions.Item>
          <Descriptions.Item label="上市日期">{s.list_date || '-'}</Descriptions.Item>
          {latest && (
            <>
              <Descriptions.Item label="最新收盘">{latest.close?.toFixed(2)}</Descriptions.Item>
              <Descriptions.Item label="涨跌幅">
                <span style={{ color: (latest.pct_chg || 0) >= 0 ? '#52c41a' : '#ff4d4f' }}>
                  {latest.pct_chg?.toFixed(2)}%
                </span>
              </Descriptions.Item>
              <Descriptions.Item label="成交量">{latest.volume?.toLocaleString()}</Descriptions.Item>
              <Descriptions.Item label="换手率">{latest.turnover_rate?.toFixed(2)}%</Descriptions.Item>
            </>
          )}
        </Descriptions>
      </Card>

      {/* 概念标签 */}
      {s.concepts && s.concepts.length > 0 && (
        <Card title="🏷️ 所属概念" style={{ marginBottom: 16 }}>
          {s.concepts.map((c: string) => (
            <Tag key={c} color="purple" style={{ margin: 2 }}>
              {c}
            </Tag>
          ))}
        </Card>
      )}

      {/* K 线简表 */}
      {bars?.data?.bars && bars.data.bars.length > 0 && (
        <Card title="📊 近期行情" style={{ marginBottom: 16 }}>
          <div style={{ overflow: 'auto', maxHeight: 300 }}>
            <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #303030' }}>
                  <th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>量</th><th>涨跌</th>
                </tr>
              </thead>
              <tbody>
                {bars.data.bars.slice(-20).map((b: any, i: number) => (
                  <tr key={i} style={{ borderBottom: '1px solid #1f1f1f', lineHeight: 2 }}>
                    <td>{b.trade_date}</td>
                    <td>{b.open?.toFixed(2)}</td>
                    <td>{b.high?.toFixed(2)}</td>
                    <td>{b.low?.toFixed(2)}</td>
                    <td style={{ fontWeight: 600 }}>{b.close?.toFixed(2)}</td>
                    <td>{(b.volume / 10000)?.toFixed(0)}万</td>
                    <td style={{ color: (b.pct_chg || 0) >= 0 ? '#52c41a' : '#ff4d4f' }}>
                      {b.pct_chg?.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* 关联资讯 */}
      {news?.data?.news && news.data.news.length > 0 && (
        <Card title="📰 关联资讯" style={{ marginBottom: 16 }}>
          <List
            dataSource={news.data.news}
            renderItem={(n: any) => (
              <List.Item>
                <List.Item.Meta
                  title={<a href={n.url} target="_blank">{n.title}</a>}
                  description={`${n.source} · ${n.published_at} · 置信度 ${n.confidence?.toFixed(2)}`}
                />
              </List.Item>
            )}
          />
        </Card>
      )}
    </div>
  )
}
