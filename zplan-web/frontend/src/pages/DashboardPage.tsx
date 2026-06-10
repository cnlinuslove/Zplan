import { useQuery } from '@tanstack/react-query'
import { Card, Statistic, Col, Row, Table, Typography, Tag } from 'antd'
import { FundOutlined, DollarOutlined, ClockCircleOutlined, CompassOutlined, RiseOutlined, FallOutlined, MinusOutlined } from '@ant-design/icons'
import api from '../api/client'

const { Title } = Typography

export default function DashboardPage() {
  const { data: stats } = useQuery({
    queryKey: ['dashboard-stats'],
    queryFn: () => api.get('/dashboard/stats'),
    refetchInterval: 30_000,
  })

  const { data: costs } = useQuery({
    queryKey: ['dashboard-costs'],
    queryFn: () => api.get('/dashboard/llm-costs', { params: { days: 30 } }),
    refetchInterval: 60_000,
  })

  const { data: pipeline } = useQuery({
    queryKey: ['dashboard-pipeline'],
    queryFn: () => api.get('/dashboard/pipeline'),
    refetchInterval: 30_000,
  })

  const { data: forecast } = useQuery({
    queryKey: ['forecast-latest'],
    queryFn: () => api.get('/forecast/latest'),
    refetchInterval: 60_000,
  })

  const s = stats?.data?.stats || {}
  const dailyCosts = costs?.data?.daily || []
  const pl = pipeline?.data?.pipeline || {}
  const fc = forecast?.data?.forecast
  const fd = fc?.forecast_data
  const md = fd?.market_direction || {}
  const DIR_TAG: Record<string, { color: string; icon: React.ReactNode }> = {
    bullish: { color: 'red', icon: <RiseOutlined /> },
    bearish: { color: 'green', icon: <FallOutlined /> },
    'range-bound': { color: 'orange', icon: <MinusOutlined /> },
  }
  const dt = DIR_TAG[md.direction] || DIR_TAG['range-bound']

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Title level={4}>📊 仪表盘</Title>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={6}>
          <Card>
            <Statistic
              title="累计选股运行"
              value={s.total_runs || 0}
              prefix={<FundOutlined />}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="累计选股条目"
              value={s.total_entries || 0}
              prefix={<FundOutlined />}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="LLM 累计费用"
              value={s.total_llm_cost_usd || 0}
              precision={4}
              prefix={<DollarOutlined />}
              suffix="USD"
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="最新行情日期"
              value={pl.latest_price_date || '-'}
              prefix={<ClockCircleOutlined />}
            />
          </Card>
        </Col>
      </Row>

      {fc && (
        <Row gutter={16} style={{ marginBottom: 24 }}>
          <Col span={24}>
            <Card
              title={<span><CompassOutlined /> 最新大盘预测</span>}
              extra={
                <span>
                  {fc.as_of_date}
                  {fc.verified && (
                    fc.verified.direction_correct
                      ? <Tag color="success" style={{ marginLeft: 8 }}>✅ 方向正确</Tag>
                      : <Tag color="error" style={{ marginLeft: 8 }}>❌ 方向偏差</Tag>
                  )}
                </span>
              }
            >
              <Row gutter={16}>
                <Col span={4}>
                  <Statistic
                    title="大盘方向"
                    valueRender={() => (
                      <Tag color={dt.color} style={{ fontSize: 16, padding: '2px 10px' }}>
                        {dt.icon} {md.direction}
                      </Tag>
                    )}
                  />
                </Col>
                <Col span={4}>
                  <Statistic title="置信度" value={md.confidence || 0} suffix="%" />
                </Col>
                <Col span={16}>
                  <div style={{ fontSize: 13, color: '#666', maxHeight: 40, overflow: 'hidden' }}>
                    {md.reasoning || ''}
                  </div>
                </Col>
              </Row>
            </Card>
          </Col>
        </Row>
      )}

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={12}>
          <Card title="数据管道状态">
            <p>最新行情: {pl.latest_price_date || '-'}</p>
            <p>最新快照: {pl.latest_snapshot_date || '-'}</p>
            <p>最新选股: {pl.latest_pick_run?.kind || '-'} @ {pl.latest_pick_run?.at || '-'}</p>
          </Card>
        </Col>
        <Col span={12}>
          <Card title="最近运行">
            {s.latest_run && (
              <>
                <p>类型: {s.latest_run.run_kind}</p>
                <p>日期: {s.latest_run.trade_date}</p>
                <p>创建: {s.latest_run.created_at}</p>
              </>
            )}
          </Card>
        </Col>
      </Row>

      {dailyCosts.length > 0 && (
        <Card title="💰 LLM 成本趋势（最近30天）">
          <Table
            dataSource={dailyCosts}
            rowKey="date"
            size="small"
            pagination={false}
            columns={[
              { title: '日期', dataIndex: 'date', key: 'date' },
              { title: '请求数', dataIndex: 'requests', key: 'requests' },
              { title: 'Input Tokens', dataIndex: 'prompt_tokens', key: 'prompt_tokens', render: (v: number) => v.toLocaleString() },
              { title: 'Output Tokens', dataIndex: 'output_tokens', key: 'output_tokens', render: (v: number) => v.toLocaleString() },
              { title: '费用(USD)', dataIndex: 'cost_usd', key: 'cost_usd', render: (v: number) => `$${v.toFixed(4)}` },
            ]}
          />
        </Card>
      )}
    </div>
  )
}
