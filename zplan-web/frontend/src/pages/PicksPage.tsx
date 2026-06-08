import { useState, useMemo, useCallback, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Table, Tag, Card, Space, Typography, Input, Segmented, Button, message } from 'antd'
import { TrophyOutlined, SearchOutlined, StarOutlined, StarFilled } from '@ant-design/icons'
import api from '../api/client'

const { Title } = Typography

export default function PicksPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [topN, setTopN] = useState<number>(30)
  const [search, setSearch] = useState('')
  const [watchingCodes, setWatchingCodes] = useState<Set<string>>(new Set())

  const { data, isLoading } = useQuery({
    queryKey: ['picks', 'latest', topN],
    queryFn: () => api.get('/picks/latest', { params: { run_kind: 'scan', top_n: topN } }),
    refetchInterval: 60_000,
    placeholderData: (prev: any) => prev,
  })

  // 加载自选列表对比
  const { data: watchlist } = useQuery({
    queryKey: ['watchlist'],
    queryFn: () => api.get('/watchlist'),
    refetchInterval: 30_000,
  })
  // 同步对照（useEffect 用于副作用）
  useEffect(() => {
    if (watchlist?.data?.items) {
      setWatchingCodes(new Set(watchlist.data.items.map((w: any) => w.ts_code)))
    }
  }, [watchlist])

  const toggleWatch = useCallback(async (ts_code: string, name: string) => {
    try {
      if (watchingCodes.has(ts_code)) {
        await api.delete(`/watchlist/${ts_code}`)
        setWatchingCodes(prev => { const n = new Set(prev); n.delete(ts_code); return n })
        message.success(`已取消关注 ${name}`)
      } else {
        await api.post('/watchlist', { ts_code })
        setWatchingCodes(prev => new Set(prev).add(ts_code))
        message.success(`已添加 ${name} 到自选`)
      }
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    } catch { message.error('操作失败') }
  }, [watchingCodes, queryClient])

  const entries = data?.data?.entries || []

  // 客户端搜索筛选
  const filtered = useMemo(() => {
    if (!search.trim()) return entries
    const q = search.trim().toLowerCase()
    return entries.filter(
      (e: any) =>
        String(e.ts_code).toLowerCase().includes(q) ||
        (e.name && e.name.toLowerCase().includes(q)),
    )
  }, [entries, search])

  const columns = [
    {
      title: '#',
      dataIndex: 'rank',
      key: 'rank',
      width: 50,
      render: (r: number) => (
        <span style={{ fontWeight: r <= 3 ? 700 : 400, color: r <= 3 ? '#faad14' : undefined }}>
          {r <= 3 && <TrophyOutlined style={{ marginRight: 4 }} />}
          {r}
        </span>
      ),
    },
    { title: '代码', dataIndex: 'ts_code', key: 'ts_code', width: 90 },
    { title: '名称', dataIndex: 'name', key: 'name', width: 90 },
    {
      title: '收盘价',
      dataIndex: 'close_price',
      key: 'close_price',
      width: 70,
      render: (v: number) => v?.toFixed(2),
    },
    {
      title: '规则分',
      dataIndex: 'rule_composite_score',
      key: 'rule_composite_score',
      width: 65,
      render: (v: number) => v?.toFixed(1),
    },
    {
      title: 'LLM分',
      dataIndex: 'llm_composite_score',
      key: 'llm_composite_score',
      width: 65,
      render: (v: number | null) => (v != null ? v.toFixed(1) : '-'),
    },
    {
      title: '最终分',
      dataIndex: 'final_composite_score',
      key: 'final_composite_score',
      width: 65,
      render: (v: number) => <strong>{v?.toFixed(1)}</strong>,
      sorter: (a: any, b: any) => (a.final_composite_score || 0) - (b.final_composite_score || 0),
    },
    {
      title: '建议',
      dataIndex: 'recommendation',
      key: 'recommendation',
      width: 80,
      render: (v: string) => {
        const colors: Record<string, string> = {
          '强烈关注': 'red', '关注': 'orange', '观望': 'default', '谨慎': 'blue', '回避': 'default',
        }
        return <Tag color={colors[v] || 'default'}>{v || '-'}</Tag>
      },
    },
    {
      title: '关注',
      key: 'watch',
      width: 55,
      render: (_: any, r: any) => (
        <Button
          type="text"
          size="small"
          icon={watchingCodes.has(r.ts_code) ? <StarFilled style={{ color: '#faad14' }} /> : <StarOutlined />}
          onClick={(e) => { e.stopPropagation(); toggleWatch(r.ts_code, r.name) }}
        />
      ),
    },
    {
      title: '判定',
      dataIndex: 'verdict',
      key: 'verdict',
      width: 60,
    },
    {
      title: '建议买入',
      dataIndex: 'predicted_buy_price',
      key: 'predicted_buy_price',
      width: 75,
      render: (v: number) => v?.toFixed(2),
    },
    {
      title: '目标价',
      dataIndex: 'predicted_target_price',
      key: 'predicted_target_price',
      width: 75,
      render: (v: number) => v?.toFixed(2),
    },
  ]

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Card style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <Space>
            <Title level={4} style={{ margin: 0 }}>🏆 选股榜单</Title>
            {data?.data && (
              <>
                <Tag color="blue">{data.data.trade_date} · {data.data.rule_version}</Tag>
                <Tag color="green">排序：规则打分 + LLM 风险微调</Tag>
              </>
            )}
          </Space>
          <Space>
            <Segmented
              value={topN}
              onChange={(val) => setTopN(val as number)}
              options={[
                { value: 10, label: 'Top 10' },
                { value: 30, label: 'Top 30' },
                { value: 50, label: 'Top 50' },
                { value: 100, label: 'Top 100' },
              ]}
            />
            <Input
              prefix={<SearchOutlined />}
              placeholder="筛选代码/名称"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              size="small"
              style={{ width: 160 }}
              allowClear
            />
          </Space>
        </Space>
      </Card>

      <div style={{ marginBottom: 8, color: '#888', fontSize: 12 }}>
        {search ? `筛选结果: ${filtered.length} / ${entries.length} 条` : `共 ${entries.length} 条`}
      </div>

      <Table
        columns={columns}
        dataSource={filtered}
        rowKey="id"
        loading={isLoading}
        size="small"
        scroll={{ x: 1000 }}
        pagination={entries.length > 50 ? { pageSize: 50, showSizeChanger: false } : false}
        onRow={(record: any) => ({
          onClick: () => navigate(`/picks/${record.id}`),
          style: { cursor: 'pointer' },
        })}
      />
    </div>
  )
}
