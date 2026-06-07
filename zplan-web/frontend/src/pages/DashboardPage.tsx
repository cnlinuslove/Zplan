import { useQuery } from '@tanstack/react-query'
import { Card, Statistic, Col, Row, Table, Typography } from 'antd'
import { FundOutlined, DollarOutlined, ClockCircleOutlined } from '@ant-design/icons'
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

  const s = stats?.data?.stats || {}
  const dailyCosts = costs?.data?.daily || []
  const pl = pipeline?.data?.pipeline || {}

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
