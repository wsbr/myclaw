import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Button, Card, DatePicker, Descriptions, Drawer, Empty, Form, Input,
  InputNumber, Modal, Popconfirm, Radio, Select, Space, Table, Tag, Typography, message,
} from 'antd'
import type { Dayjs } from 'dayjs'
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'
import { CalendarClock, History, Pause, Play, Plus, Rocket, Trash2 } from 'lucide-react'

import { agentsApi } from '@/api/agents'
import {
  AgentSchedule, AgentScheduleFire, AgentScheduleInput, ScheduleType, schedulesApi,
} from '@/api/schedules'
import { displayLabel } from '@/lib/presentation'


const { Title, Paragraph, Text } = Typography
dayjs.extend(utc)
dayjs.extend(timezone)
const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'

type FormValues = {
  agent_id: string
  name: string
  prompt: string
  schedule_type: ScheduleType
  run_at?: Dayjs
  cron_expression?: string
  interval_minutes?: number
  timezone: string
  version_policy: 'latest_published' | 'pinned'
  overlap_policy: 'skip' | 'queue_one' | 'allow'
  misfire_policy: 'skip' | 'fire_once'
  misfire_grace_seconds: number
}

function formToInput(values: FormValues): AgentScheduleInput {
  return {
    agent_id: values.agent_id,
    name: values.name.trim(), prompt: values.prompt.trim(),
    schedule_type: values.schedule_type,
    run_at: values.schedule_type === 'once' ? values.run_at!.toISOString() : null,
    cron_expression: values.schedule_type === 'cron' ? values.cron_expression!.trim() : null,
    interval_seconds: values.schedule_type === 'interval' ? values.interval_minutes! * 60 : null,
    timezone: values.timezone,
    end_at: null,
    version_policy: values.version_policy,
    pinned_agent_version_id: null,
    overlap_policy: values.overlap_policy,
    misfire_policy: values.misfire_policy,
    misfire_grace_seconds: values.misfire_grace_seconds,
  }
}

function statusColor(status: string) {
  if (status === 'active' || status === 'succeeded') return 'green'
  if (status === 'running' || status === 'queued' || status === 'dispatching') return 'blue'
  if (status === 'paused' || status === 'deferred') return 'gold'
  if (status === 'failed') return 'red'
  return 'default'
}

function scheduleText(item: AgentSchedule) {
  if (item.schedule_type === 'once') return `单次 · ${dayjs.utc(item.run_at).tz(item.timezone).format('YYYY-MM-DD HH:mm')}`
  if (item.schedule_type === 'cron') return `Cron · ${item.cron_expression}`
  return `每 ${Math.round((item.interval_seconds || 0) / 60)} 分钟`
}

export default function SchedulesPage() {
  const queryClient = useQueryClient()
  const [form] = Form.useForm<FormValues>()
  const [editing, setEditing] = useState<AgentSchedule | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [historyItem, setHistoryItem] = useState<AgentSchedule | null>(null)
  const scheduleType = Form.useWatch('schedule_type', form)

  const schedules = useQuery({
    queryKey: ['agent-schedules'], queryFn: schedulesApi.list, refetchInterval: 5000,
  })
  const agents = useQuery({ queryKey: ['agents'], queryFn: agentsApi.list })
  const fires = useQuery({
    queryKey: ['agent-schedule-fires', historyItem?.id],
    queryFn: () => schedulesApi.fires(historyItem!.id),
    enabled: !!historyItem,
    refetchInterval: query => query.state.data?.some(item =>
      ['queued', 'deferred', 'dispatching', 'running'].includes(item.status),
    ) ? 2000 : false,
  })

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['agent-schedules'] }),
      queryClient.invalidateQueries({ queryKey: ['agent-schedule-fires'] }),
      queryClient.invalidateQueries({ queryKey: ['schedule-notifications'] }),
    ])
  }

  const save = useMutation({
    mutationFn: (values: FormValues) => editing
      ? schedulesApi.update(editing, formToInput(values))
      : schedulesApi.create(formToInput(values)),
    onSuccess: async () => {
      message.success(editing ? '定时任务已更新' : '定时任务已创建')
      setModalOpen(false); setEditing(null); form.resetFields(); await refresh()
    },
    onError: () => message.error('保存失败，请检查时间、Cron 表达式或任务版本是否已变化'),
  })
  const state = useMutation({
    mutationFn: (item: AgentSchedule) => item.status === 'active'
      ? schedulesApi.pause(item) : schedulesApi.resume(item),
    onSuccess: async item => { message.success(item.status === 'active' ? '任务已恢复' : '任务已暂停'); await refresh() },
    onError: () => message.error('状态更新失败，任务可能已被其他操作修改'),
  })
  const runNow = useMutation({
    mutationFn: schedulesApi.runNow,
    onSuccess: async (_, item) => { message.success('已进入执行队列'); setHistoryItem(item); await refresh() },
    onError: () => message.error('立即执行请求失败'),
  })
  const disable = useMutation({
    mutationFn: schedulesApi.disable,
    onSuccess: async () => { message.success('任务已停用，历史记录仍保留'); await refresh() },
    onError: () => message.error('停用失败，任务版本可能已变化'),
  })

  const openCreate = () => {
    setEditing(null)
    form.setFieldsValue({
      schedule_type: 'once', run_at: dayjs().add(10, 'minute'),
      timezone: localTimezone, version_policy: 'latest_published',
      overlap_policy: 'skip', misfire_policy: 'fire_once', misfire_grace_seconds: 300,
    })
    setModalOpen(true)
  }
  const openEdit = (item: AgentSchedule) => {
    setEditing(item)
    form.setFieldsValue({
      agent_id: item.agent_id, name: item.name, prompt: item.prompt,
      schedule_type: item.schedule_type,
      run_at: item.run_at ? dayjs.utc(item.run_at).tz(item.timezone) : undefined,
      cron_expression: item.cron_expression || undefined,
      interval_minutes: item.interval_seconds ? item.interval_seconds / 60 : undefined,
      timezone: item.timezone, version_policy: item.version_policy,
      overlap_policy: item.overlap_policy, misfire_policy: item.misfire_policy,
      misfire_grace_seconds: item.misfire_grace_seconds,
    })
    setModalOpen(true)
  }

  const activeCount = useMemo(
    () => schedules.data?.filter(item => item.status === 'active').length || 0,
    [schedules.data],
  )

  useEffect(() => {
    if (!modalOpen) form.resetFields()
  }, [modalOpen, form])

  return <div className="app-page">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <Title level={2} className="!mb-1"><Space><CalendarClock className="size-6" />定时任务</Space></Title>
        <Paragraph type="secondary" className="!mb-0">
          到点后创建或继续一次标准智能体对话。模型、工具、审批、审计与群组涌现均沿用普通运行链路。
        </Paragraph>
      </div>
      <Button type="primary" icon={<Plus className="size-4" />} onClick={openCreate}>新建任务</Button>
    </div>

    <Alert type="info" showIcon message={`${activeCount} 个任务正在调度`}
      description="任务定义按所选 IANA 时区计算，数据库统一保存 UTC。多节点 Scheduler 使用租约与唯一触发键防止重复执行；暂停不会中断已经开始的运行。" />

    <Card className="overflow-hidden" styles={{ body: { padding: 0 } }}>
      <Table rowKey="id" loading={schedules.isLoading} dataSource={schedules.data || []}
        locale={{ emptyText: <Empty description="还没有定时任务" /> }}
        columns={[
          { title: '任务', render: (_: unknown, item: AgentSchedule) => <div>
            <Text strong>{item.name}</Text><Text type="secondary" className="mt-1 block text-xs">{item.agent_name}</Text>
          </div> },
          { title: '计划', render: (_: unknown, item: AgentSchedule) => <div>
            <Text>{scheduleText(item)}</Text><Text type="secondary" className="mt-1 block text-xs">{item.timezone}</Text>
          </div> },
          { title: '上下文', render: (_: unknown, item: AgentSchedule) =>
            item.context_mode === 'continue_session' ? <Tag color="purple">继续原对话</Tag> : <Tag>独立新对话</Tag> },
          { title: '下次运行', render: (_: unknown, item: AgentSchedule) => item.next_fire_at
            ? <span>{dayjs.utc(item.next_fire_at).tz(item.timezone).format('YYYY-MM-DD HH:mm:ss')}</span> : '—' },
          { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{displayLabel(value)}</Tag> },
          { title: '已执行', dataIndex: 'total_fire_count', width: 90 },
          { title: '操作', width: 330, render: (_: unknown, item: AgentSchedule) => <Space wrap>
            <Button size="small" onClick={() => openEdit(item)}>编辑</Button>
            <Button size="small" icon={<Rocket className="size-3.5" />} loading={runNow.isPending} onClick={() => runNow.mutate(item)}>立即执行</Button>
            {item.status !== 'completed' && <Button size="small"
              icon={item.status === 'active' ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
              loading={state.isPending} onClick={() => state.mutate(item)}>
              {item.status === 'active' ? '暂停' : '恢复'}
            </Button>}
            <Button size="small" icon={<History className="size-3.5" />} onClick={() => setHistoryItem(item)}>历史</Button>
            <Popconfirm title="停用这个任务？" description="未来触发会取消，已产生的会话和历史仍保留。"
              onConfirm={() => disable.mutate(item)}>
              <Button danger size="small" icon={<Trash2 className="size-3.5" />} />
            </Popconfirm>
          </Space> },
        ]} />
    </Card>

    <Modal title={editing ? '编辑定时任务' : '新建定时任务'} open={modalOpen}
      onCancel={() => setModalOpen(false)} onOk={() => form.submit()}
      confirmLoading={save.isPending} okText="保存" width={720} destroyOnClose>
      <Form form={form} layout="vertical" onFinish={values => save.mutate(values)}>
        <div className="grid gap-x-4 md:grid-cols-2">
          <Form.Item name="name" label="任务名称" rules={[{ required: true }, { max: 200 }]}>
            <Input placeholder="例如：每日风险摘要" />
          </Form.Item>
          <Form.Item name="agent_id" label="执行智能体" rules={[{ required: true }]}>
            <Select disabled={!!editing} showSearch optionFilterProp="label" placeholder="选择已发布智能体"
              options={(agents.data || []).filter(item => item.status === 'published').map(item => ({ value: item.id, label: item.name }))} />
          </Form.Item>
        </div>
        <Form.Item name="prompt" label="任务指令" rules={[{ required: true }, { max: 16000 }]}>
          <Input.TextArea rows={5} showCount maxLength={16000} placeholder="描述每次触发时希望智能体完成的工作。" />
        </Form.Item>
        <Form.Item name="schedule_type" label="触发方式" rules={[{ required: true }]}>
          <Radio.Group optionType="button" buttonStyle="solid" options={[
            { value: 'once', label: '单次' }, { value: 'cron', label: 'Cron' }, { value: 'interval', label: '固定间隔' },
          ]} />
        </Form.Item>
        {scheduleType === 'once' && <Form.Item name="run_at" label="运行时间" rules={[{ required: true }]}>
          <DatePicker showTime className="w-full" disabledDate={date => date.endOf('day').isBefore(dayjs())} />
        </Form.Item>}
        {scheduleType === 'cron' && <Form.Item name="cron_expression" label="Cron 表达式（5 段）"
          extra="例如 0 9 * * 1-5 表示工作日 09:00，按下方时区解释。" rules={[{ required: true }]}>
          <Input placeholder="0 9 * * 1-5" />
        </Form.Item>}
        {scheduleType === 'interval' && <Form.Item name="interval_minutes" label="间隔分钟数"
          rules={[{ required: true }]}>
          <InputNumber min={1} max={525600} precision={0} className="w-full" />
        </Form.Item>}
        <div className="grid gap-x-4 md:grid-cols-2">
          <Form.Item name="timezone" label="时区" rules={[{ required: true }]}>
            <Input placeholder="Asia/Shanghai" />
          </Form.Item>
          <Form.Item name="version_policy" label="智能体版本" rules={[{ required: true }]}>
            <Select options={[
              { value: 'latest_published', label: '每次使用最新已发布版本' },
              { value: 'pinned', label: '固定当前已发布版本' },
            ]} />
          </Form.Item>
          <Form.Item name="overlap_policy" label="上次尚未完成" rules={[{ required: true }]}>
            <Select options={[
              { value: 'skip', label: '跳过本次（推荐）' },
              { value: 'queue_one', label: '最多补跑一次' },
              { value: 'allow', label: '允许并行执行' },
            ]} />
          </Form.Item>
          <Form.Item name="misfire_policy" label="调度器停机后恢复" rules={[{ required: true }]}>
            <Select options={[
              { value: 'fire_once', label: '合并补跑一次' },
              { value: 'skip', label: '超过宽限期则跳过' },
            ]} />
          </Form.Item>
        </div>
        <Form.Item name="misfire_grace_seconds" label="误点宽限秒数" rules={[{ required: true }]}>
          <InputNumber min={0} max={86400} precision={0} className="w-full" />
        </Form.Item>
      </Form>
    </Modal>

    <Drawer title={historyItem ? `${historyItem.name} · 执行历史` : '执行历史'} width={720}
      open={!!historyItem} onClose={() => setHistoryItem(null)}>
      {historyItem && <Descriptions size="small" column={2} className="mb-5" items={[
        { key: 'agent', label: '智能体', children: historyItem.agent_name },
        { key: 'schedule', label: '计划', children: scheduleText(historyItem) },
        { key: 'policy', label: '重叠策略', children: displayLabel(historyItem.overlap_policy) },
        { key: 'version', label: '版本策略', children: historyItem.version_policy === 'pinned' ? '固定版本' : '最新已发布版本' },
        { key: 'context', label: '上下文', children: historyItem.context_mode === 'continue_session' ? '继续原对话' : '独立新对话' },
      ]} />}
      <Table<AgentScheduleFire> rowKey="id" size="small" loading={fires.isLoading} dataSource={fires.data || []}
        expandable={{ expandedRowRender: item => <div className="space-y-2">
          {item.result_preview && <Paragraph className="whitespace-pre-wrap">{item.result_preview}</Paragraph>}
          {(item.error_detail || item.reason_code) && <Text type="danger">{item.error_detail || item.reason_code}</Text>}
        </div> }} columns={[
          { title: '计划时间', render: (_: unknown, item) => dayjs.utc(item.scheduled_for).tz(historyItem?.timezone || localTimezone).format('MM-DD HH:mm:ss') },
          { title: '来源', dataIndex: 'trigger_kind', render: (value: string) => displayLabel(value) },
          { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{displayLabel(value)}</Tag> },
          { title: '会话', render: (_: unknown, item) => item.session_id
            ? <Button type="link" size="small" href={`/agents/${item.agent_id}/chat/${item.session_id}`}>打开</Button> : '—' },
        ]} />
    </Drawer>
  </div>
}
