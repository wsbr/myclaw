import { useState } from 'react'
import { Alert, Button, Card, Descriptions, Empty, Input, List, Space, Tag, Typography } from 'antd'
import type {
  AgentInvocation, ApprovalAction, Artifact, Run, UserSession, WorkingPlan,
} from '@/api/sessions'

interface Props {
  session: UserSession | undefined
  run: Run | undefined
  plan: WorkingPlan | undefined
  collaborators: AgentInvocation[]
  approval: ApprovalAction | undefined
  artifacts: Artifact[]
  busy: boolean
  onContinue: (instruction: string) => Promise<void>
  onApprove: () => Promise<void>
  onReject: (reason?: string) => Promise<void>
  onExit: () => Promise<void>
  onDownload: (artifact: Artifact) => Promise<void>
}

const itemColor: Record<string, string> = {
  completed: 'success', in_progress: 'processing', pending: 'default',
}

const invocationColor: Record<string, string> = {
  succeeded: 'success', failed: 'error', rejected: 'error', running: 'processing',
  queued: 'warning', created: 'warning', cancelled: 'default', timed_out: 'error',
}

export default function PlanPanel({
  session, run, plan, collaborators, approval, artifacts, busy,
  onContinue, onApprove, onReject, onExit, onDownload,
}: Props) {
  const [instruction, setInstruction] = useState('')
  const waitingForDecision = run?.status === 'waiting_approval' && Boolean(approval)
  const readonly = session?.permission_mode === 'plan_readonly'
  const canExit = readonly || !run || ['completed', 'cancelled', 'failed'].includes(run.status)

  if (!session || session.interaction_mode !== 'plan') {
    return <div className="py-20 text-center">
      <div className="mb-3 text-3xl">◇</div>
      <p className="text-lg font-semibold">进入 Plan 模式</p>
      <p className="mt-2 text-sm text-muted-foreground">
        项目经理会先只读调查并维护动态 WorkingPlan；批准前不会执行写入、execute 或 publish。
      </p>
    </div>
  }

  return <div className="space-y-4">
    <Card title={<Space><span>Plan 权限与执行</span><Tag color={readonly ? 'gold' : 'green'}>{session.permission_mode}</Tag></Space>}
      extra={canExit && <Button size="small" loading={busy} onClick={onExit}>退出 Plan</Button>}>
      <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
        <Descriptions.Item label="交互模式">{session.interaction_mode} · r{session.interaction_revision}</Descriptions.Item>
        <Descriptions.Item label="权限版本">{session.permission_mode} · r{session.permission_revision}</Descriptions.Item>
        <Descriptions.Item label="Run 状态">{run ? <Space><Tag>{run.status}</Tag><span>{run.wait_reason || run.lifecycle}</span></Space> : '等待目标'}</Descriptions.Item>
        <Descriptions.Item label="同一执行上下文">{run?.root_work_item_id ? <Typography.Text code>{run.root_work_item_id.slice(0, 12)}</Typography.Text> : '—'}</Descriptions.Item>
      </Descriptions>
      {readonly && <Alert className="mt-4" type="warning" showIcon
        message="批准前为 plan_readonly：可以读取、澄清、更新计划和调用只读协作者；副作用在 CapabilitySnapshot 与 Dispatcher 两层阻断。" />}
      {!readonly && run && !['completed', 'cancelled', 'failed'].includes(run.status) && <Alert className="mt-4" type="success" showIcon
        message="当前 revision 已批准；同一 Session、Run 和根 WorkItem 已恢复 direct 权限并动态实施。" />}
    </Card>

    <Card title={<Space><span>WorkingPlan</span>{plan && <Tag color="purple">revision {plan.revision}</Tag>}</Space>}>
      {!plan ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={run ? '项目经理正在调查并形成计划…' : '输入目标后开始只读调查'} /> : <>
        <p className="mb-3 text-sm text-muted-foreground">{plan.summary}</p>
        <List size="small" dataSource={plan.items} renderItem={(item, index) => <List.Item>
          <div className="flex w-full items-start gap-2">
            <Tag color={itemColor[item.status]}>{item.status}</Tag>
            <span><span className="mr-2 text-muted-foreground">{index + 1}.</span>{item.text}</span>
          </div>
        </List.Item>} />
        {plan.explanation && <Alert className="mt-3" type="info" message={plan.explanation} />}
      </>}
    </Card>

    <Card title={<Space><span>Required Action</span>{waitingForDecision && <Tag color="warning">需要决定</Tag>}</Space>}>
      {waitingForDecision ? <>
        <p className="mb-2 font-medium">{approval?.presentation.title}</p>
        <p className="mb-3 text-sm text-muted-foreground">{approval?.presentation.summary}</p>
        <Descriptions size="small" column={1} className="mb-3">
          <Descriptions.Item label="绑定 Plan revision">{approval?.presentation.plan_revision}</Descriptions.Item>
          <Descriptions.Item label="绑定 permission revision">{approval?.presentation.permission_revision}</Descriptions.Item>
          <Descriptions.Item label="Subject hash"><Typography.Text code copyable>{approval?.subject_hash}</Typography.Text></Descriptions.Item>
        </Descriptions>
        <Space>
          <Button type="primary" loading={busy} onClick={onApprove}>批准并在同一 Session 实施</Button>
          <Button danger loading={busy} onClick={() => onReject(instruction.trim() || undefined)}>拒绝当前计划</Button>
        </Space>
      </> : <p className="text-sm text-muted-foreground">
        {run?.status === 'waiting_user' ? '项目经理正在等待澄清。' : '当前没有待处理的 Plan 批准。'}
      </p>}
      {readonly && plan && ['waiting_approval', 'waiting_user'].includes(run?.status || '') && <Space.Compact className="mt-4 w-full">
        <Input value={instruction} onChange={event => setInstruction(event.target.value)}
          placeholder="继续讨论或修改方向，例如：先验证迁移回滚，再实现 UI" />
        <Button loading={busy} disabled={!instruction.trim()} onClick={async () => {
          await onContinue(instruction.trim()); setInstruction('')
        }}>修改方向</Button>
      </Space.Compact>}
    </Card>

    <Card title={<Space><span>协作者</span><Tag>{collaborators.length}</Tag></Space>}>
      {collaborators.length === 0 ? <p className="text-sm text-muted-foreground">项目经理尚未按需调用候选 Agent。</p> : <List
        size="small" dataSource={collaborators} renderItem={item => <List.Item>
          <div className="w-full">
            <Space><Typography.Text code>{item.target_agent_id.slice(0, 12)}</Typography.Text><Tag color={invocationColor[item.status]}>{item.status}</Tag></Space>
            <div className="mt-1 text-sm">{item.request_text}</div>
            {(item.result_summary || item.error_code) && <div className="mt-1 text-xs text-muted-foreground">{item.result_summary || item.error_code}</div>}
          </div>
        </List.Item>}
      />}
    </Card>

    <Card title={<Space><span>Artifacts</span><Tag>{artifacts.length}</Tag></Space>}>
      {artifacts.length === 0 ? <p className="text-sm text-muted-foreground">尚无 Artifact；批准实施后产物会在这里出现。</p> : <List
        size="small" dataSource={artifacts} renderItem={artifact => <List.Item
          actions={[<Button key="download" size="small" disabled={artifact.status !== 'published'} onClick={() => onDownload(artifact)}>下载</Button>]}>
          <List.Item.Meta title={artifact.name} description={`${artifact.mime_type} · ${artifact.size_bytes} bytes · ${artifact.status}`} />
        </List.Item>}
      />}
    </Card>
  </div>
}
