import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Card, Descriptions, Tag, Button, Spin, Typography, Space, List, Image, message } from 'antd'
import { ArrowLeftOutlined, StarOutlined, StarFilled, FundOutlined } from '@ant-design/icons'
import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import ResearchButton from '../components/ResearchButton'
import api from '../api/client'

const { Title } = Typography

export default function StockDetailPage() {
  const { tsCode } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [watching, setWatching] = useState(false)
  const [watchLoading, setWatchLoading] = useState(false)
  const [showChart, setShowChart] = useState(true)

  // 查看是否已在自选
  const { data: watchlist } = useQuery({
    queryKey: ['watchlist'],
    queryFn: () => api.get('/watchlist'),
  })
  useEffect(() => {
    if (watchlist?.data?.items) {
      const found = watchlist.data.items.some((w: any) => w.ts_code === tsCode)
      setWatching(found)
    }
  }, [watchlist, tsCode])

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

  // 直接按股票代码查最新研报
  const { data: pickEntry } = useQuery({
    queryKey: ['stock-pick', tsCode],
    queryFn: async () => {
      const res = await api.get(`/picks/stock/${tsCode}`)
      return res.data?.entry || null
    },
    enabled: !!tsCode,
  })

  if (isLoading) return <Spin style={{ display: 'block', margin: '40vh auto' }} />
  const s = detail?.data?.stock
  if (!s) return <div style={{ padding: 24 }}>未找到 {tsCode}</div>

  const latest = s.latest

  async function toggleWatchlist() {
    setWatchLoading(true)
    try {
      if (watching) {
        await api.delete(`/watchlist/${tsCode}`)
        setWatching(false)
        message.success('已取消关注')
      } else {
        await api.post('/watchlist', { ts_code: tsCode })
        setWatching(true)
        message.success(`已添加 ${s?.name || tsCode} 到自选`)
      }
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    } catch {
      message.error('操作失败')
    } finally {
      setWatchLoading(false)
    }
  }

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>返回</Button>
        <Button
          icon={watching ? <StarFilled style={{ color: '#faad14' }} /> : <StarOutlined />}
          onClick={toggleWatchlist}
          loading={watchLoading}
          type={watching ? 'primary' : 'default'}
        >
          {watching ? '已关注' : '加入自选'}
        </Button>
        {pickEntry && (
          <Button
            type="primary"
            icon={<FundOutlined />}
            onClick={() => navigate(`/picks/${pickEntry.id}`)}
          >
            查看选股分析
          </Button>
        )}
      </Space>

      <Card style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          {s.name} ({s.ts_code})
          <Tag style={{ marginLeft: 8 }}>{s.market === 'a' ? 'A股' : '港股'}</Tag>
          {pickEntry && (
            <Tag color="orange" style={{ marginLeft: 4 }}>
              {pickEntry.recommendation || '-'}
            </Tag>
          )}
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
          {pickEntry && (
            <>
              <Descriptions.Item label="选股评分">
                <strong>{pickEntry.final_composite_score?.toFixed(1)}</strong>
              </Descriptions.Item>
              <Descriptions.Item label="建仓价">{pickEntry.predicted_buy_price?.toFixed(2)}</Descriptions.Item>
              <Descriptions.Item label="目标价">{pickEntry.predicted_target_price?.toFixed(2)}</Descriptions.Item>
              <Descriptions.Item label="止损价">{pickEntry.predicted_stop_loss?.toFixed(2)}</Descriptions.Item>
            </>
          )}
        </Descriptions>
      </Card>

      {/* 技术趋势图——默认展示 */}
      <Card
        title="📈 技术趋势图 (K线 + MACD + 均线 + 信号)"
        style={{ marginBottom: 16 }}
        extra={
          <Button size="small" onClick={() => setShowChart(!showChart)}>
            {showChart ? '收起' : '展开'}
          </Button>
        }
      >
        {showChart && (
          <Image
            src={`/api/v1/market/stocks/${tsCode}/chart?lookback=120`}
            alt="K线图"
            style={{ width: '100%', maxHeight: 700, objectFit: 'contain', background: '#0d0d0d', minHeight: 200 }}
            preview={{ mask: '点击放大' }}
            placeholder={
              <div style={{ padding: 60, textAlign: 'center', color: '#888', background: '#0d0d0d', minHeight: 200 }}>
                <Spin /> <span style={{ marginLeft: 12 }}>趋势图生成中...</span>
              </div>
            }
          />
        )}
      </Card>

      {/* 选股分析简版 + 链接 */}
      {pickEntry ? (
        <Card
          title="📝 选股分析"
          extra={<Button size="small" onClick={() => navigate(`/picks/${pickEntry.id}`)}>查看完整报告 →</Button>}
          style={{ marginBottom: 16 }}
        >
          <Descriptions size="small" column={3}>
            <Descriptions.Item label="规则分">{pickEntry.rule_composite_score?.toFixed(1)}</Descriptions.Item>
            <Descriptions.Item label="LLM分">{pickEntry.llm_composite_score?.toFixed(1) || '-'}</Descriptions.Item>
            <Descriptions.Item label="最终分"><strong>{pickEntry.final_composite_score?.toFixed(1)}</strong></Descriptions.Item>
            <Descriptions.Item label="建议">{pickEntry.recommendation}</Descriptions.Item>
            <Descriptions.Item label="判定">{pickEntry.verdict}</Descriptions.Item>
            <Descriptions.Item label="排名">#{pickEntry.rank}</Descriptions.Item>
          </Descriptions>
          {pickEntry.markdown_report ? (
            <div style={{ lineHeight: 1.8, maxHeight: 400, overflow: 'auto', marginTop: 12 }}>
              <ReactMarkdown>{pickEntry.markdown_report}</ReactMarkdown>
            </div>
          ) : (
            <div style={{ marginTop: 12 }}>
              <ResearchButton tsCode={tsCode!} />
            </div>
          )}
        </Card>
      ) : (
        <Card title="📝 选股分析" style={{ marginBottom: 16 }}>
          <p>该股票暂未出现在最新选股榜单中。</p>
          <ResearchButton tsCode={tsCode!} label="🤖 一键生成深度研报" />
        </Card>
      )}

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

      {/* K 线数据表 */}
      {bars?.data?.bars && bars.data.bars.length > 0 && (
        <Card title="📊 近期行情数据" style={{ marginBottom: 16 }}>
          <div style={{ overflow: 'auto', maxHeight: 300 }}>
            <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #303030' }}>
                  <th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>量(万)</th><th>涨跌</th>
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
                  title={<a href={n.url} target="_blank" rel="noopener noreferrer">{n.title}</a>}
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
