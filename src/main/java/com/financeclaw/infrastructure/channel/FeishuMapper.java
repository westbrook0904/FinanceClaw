package com.financeclaw.infrastructure.channel;

import com.financeclaw.domain.Feishu.*;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import org.apache.ibatis.annotations.*;

/**
 * 飞书身份绑定、入站事件去重和出站通知状态的 SQL 映射。
 *
 * <p>绑定与通知需要在同一事务中按 binding → notice 加锁并校验 revision。 出站状态只描述本地掌握的证据：SDK 返回不等于用户已收到，unknown 不自动重发。
 * 外部平台身份与业务 owner 的映射以绑定表为准，不使用请求正文中的业务身份。
 */
@Mapper
public interface FeishuMapper {
    @Insert(
            "INSERT INTO channel.link_codes(code_hash,tenant,subject,conversation_id)"
                    + " VALUES(#{hash},#{owner.tenant},#{owner.subject},#{conversation})")
    void insertLinkCode(
            @Param("hash") String hash,
            @Param("owner") Owner owner,
            @Param("conversation") String conversation);

    @Select(
            "SELECT tenant,subject,conversation_id,(NOT used AND expires_at>clock_timestamp()) AS"
                    + " live FROM channel.link_codes WHERE code_hash=#{hash} FOR UPDATE")
    Link getLinkCodeForUpdate(String hash);

    @Update("UPDATE channel.link_codes SET used=true WHERE code_hash=#{hash}")
    void updateLinkCodeUsed(String hash);

    /** 按飞书租户、用户和会话锁定绑定；不接受消息正文冒充业务 owner。 */
    @Select(
            "SELECT * FROM channel.bindings WHERE tenant_key=#{tenant} AND open_id=#{user} AND"
                    + " chat_id=#{chat} FOR UPDATE")
    Binding getBindingByChannelIdentityForUpdate(
            @Param("tenant") String tenant, @Param("user") String user, @Param("chat") String chat);

    @Select("SELECT * FROM channel.bindings WHERE id=#{id} FOR UPDATE")
    Binding getBindingForUpdate(String id);

    @Insert(
            "INSERT INTO"
                    + " channel.bindings(id,tenant_key,open_id,chat_id,tenant,subject,conversation_id)"
                    + " VALUES(#{id},#{tenantKey},#{openId},#{chatId},#{tenant},#{subject},#{conversationId})")
    void insertBinding(Binding binding);

    @Update(
            "UPDATE channel.bindings SET"
                    + " conversation_id=#{conversation},revision=revision+1,active_turn=NULL WHERE"
                    + " id=#{id}")
    void updateBindingConversation(
            @Param("id") String id, @Param("conversation") String conversation);

    @Update("UPDATE channel.bindings SET muted=#{mute} WHERE id=#{id}")
    void updateBindingMuted(@Param("id") String id, @Param("mute") boolean mute);

    @Update("UPDATE channel.bindings SET active_turn=#{turn} WHERE id=#{id}")
    void updateBindingActiveTurn(@Param("id") String id, @Param("turn") String turn);

    @Select("SELECT binding_id,request_hash,receipt FROM channel.inbox WHERE event_id=#{id}")
    Inbox getInboxEventById(String id);

    @Insert(
            "INSERT INTO channel.inbox(event_id,binding_id,request_hash,receipt)"
                    + " VALUES(#{id},#{binding},#{hash},#{receipt})")
    void insertInboxReceipt(
            @Param("id") String id,
            @Param("binding") String binding,
            @Param("hash") String hash,
            @Param("receipt") String receipt);

    /**
     * 无锁发现阶段跳过没有有效 pending 交互的 waiting 项，避免空文案占满有界批次并阻止空闲退避。 该 EXISTS 只减少无效候选，不授予投递资格；选中后仍须在逐
     * binding 事务内按最新绑定读取 Turn、pending 与 live 并渲染。 非 waiting 状态和既有通知去重/抑制重激活规则保持，不缓存有效期或跨事务复用查询结果。
     */
    @Select(
            "SELECT b.id FROM channel.bindings b JOIN business.turns t ON t.id=b.active_turn WHERE"
                    + " NOT b.muted AND t.status IN"
                    + " ('waiting','blocked','completed','failed','cancelled')"
                    + " AND (t.status<>'waiting' OR EXISTS(SELECT 1 FROM business.interactions i"
                    + " WHERE i.turn_id=t.id AND i.status='pending'"
                    + " AND i.expires_at>clock_timestamp())) AND NOT EXISTS(SELECT 1"
                    + " FROM channel.notices n WHERE n.turn_id=t.id AND n.revision=t.revision AND"
                    + " n.status<>'suppressed') ORDER BY t.created_at LIMIT 20")
    List<String> listBindingIdsRequiringProjection();

    @Insert(
            "INSERT INTO channel.notices(id,turn_id,binding_id,binding_revision,revision,payload)"
                    + " VALUES(#{id},#{turnId},#{bindingId},#{bindingRevision},#{revision},#{payload})"
                    + " ON CONFLICT(turn_id,revision) DO UPDATE SET status='pending',reason=NULL WHERE"
                    + " channel.notices.status='suppressed' AND"
                    + " channel.notices.binding_revision=excluded.binding_revision")
    void insertOrReactivateNotice(Notice notice);

    /** 无锁观察阶段只传递领取定位所需的两个标识，不重复读取通知正文或版本。 候选不是发送资格或授权快照；完整通知与路由仍须取自后续锁定读取，缺行时不能回退到候选。 */
    record NoticeCandidate(String id, String bindingId) {}

    /** 只观察最早 pending 通知的位置；原队列谓词、排序和单条上限保持，真正领取仍需锁内重验。 */
    @Select(
            "SELECT id,binding_id FROM channel.notices WHERE status='pending' ORDER BY created_at"
                    + " LIMIT 1")
    @ConstructorArgs({
        @Arg(column = "id", javaType = String.class),
        @Arg(column = "binding_id", javaType = String.class)
    })
    NoticeCandidate getNextPendingNotice();

    @Select(
            "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason FROM"
                    + " channel.notices WHERE id=#{id} FOR UPDATE")
    Notice getNoticeForUpdate(String id);

    /** 同一领取事务的类型化行投影，只按 SQL 实际返回的标量构造 Binding/Notice。 不从候选、旧绑定或应用时间合成状态，也不承载跨事务授权缓存。 */
    record LockedNoticeRow(
            String bindingId,
            String tenantKey,
            String openId,
            String chatId,
            String tenant,
            String subject,
            String conversationId,
            long bindingRevision,
            boolean muted,
            String activeTurn,
            String noticeId,
            String noticeTurnId,
            String noticeBindingId,
            long noticeBindingRevision,
            long noticeRevision,
            String noticePayload,
            String noticeStatus,
            String noticeReason) {
        public Binding binding() {
            return new Binding(
                    bindingId,
                    tenantKey,
                    openId,
                    chatId,
                    tenant,
                    subject,
                    conversationId,
                    bindingRevision,
                    muted,
                    activeTurn);
        }

        public Notice notice() {
            return new Notice(
                    noticeId,
                    noticeTurnId,
                    noticeBindingId,
                    noticeBindingRevision,
                    noticeRevision,
                    noticePayload,
                    noticeStatus,
                    noticeReason);
        }
    }

    /**
     * 只合并原领取事务中的两个锁定读取。locked_notice 依赖 locked_binding 的实际行， 必须先取得 binding 行锁，才能产生待锁 notice
     * 的连接结果；不在 SQL 预筛 status、静音或版本。 两级 MATERIALIZED 不建立新的 PostgreSQL 语句快照；行锁等待后的更新版本由 PG 核验， 后续
     * owner 范围的 Turn 读取仍必须使用原来独立的新 SQL，不能移进此 CTE。
     */
    @Select(
            """
WITH locked_binding AS MATERIALIZED (
    SELECT b.* FROM channel.bindings b
    WHERE b.id=#{bindingId} FOR UPDATE OF b
), locked_notice AS MATERIALIZED (
    SELECT n.* FROM locked_binding b CROSS JOIN channel.notices n
    WHERE n.id=#{noticeId} FOR UPDATE OF n
)
SELECT b.id AS binding_id,b.tenant_key,b.open_id,b.chat_id,b.tenant,b.subject,b.conversation_id,
       b.revision AS binding_revision,b.muted,b.active_turn,
       n.id AS notice_id,n.turn_id AS notice_turn_id,n.binding_id AS notice_binding_id,
       n.binding_revision AS notice_binding_revision,n.revision AS notice_revision,
       n.payload AS notice_payload,n.status AS notice_status,n.reason AS notice_reason
FROM locked_binding b CROSS JOIN locked_notice n
""")
    @Options(flushCache = Options.FlushCachePolicy.TRUE, useCache = false)
    @ConstructorArgs({
        @Arg(column = "binding_id", javaType = String.class),
        @Arg(column = "tenant_key", javaType = String.class),
        @Arg(column = "open_id", javaType = String.class),
        @Arg(column = "chat_id", javaType = String.class),
        @Arg(column = "tenant", javaType = String.class),
        @Arg(column = "subject", javaType = String.class),
        @Arg(column = "conversation_id", javaType = String.class),
        @Arg(column = "binding_revision", javaType = long.class),
        @Arg(column = "muted", javaType = boolean.class),
        @Arg(column = "active_turn", javaType = String.class),
        @Arg(column = "notice_id", javaType = String.class),
        @Arg(column = "notice_turn_id", javaType = String.class),
        @Arg(column = "notice_binding_id", javaType = String.class),
        @Arg(column = "notice_binding_revision", javaType = long.class),
        @Arg(column = "notice_revision", javaType = long.class),
        @Arg(column = "notice_payload", javaType = String.class),
        @Arg(column = "notice_status", javaType = String.class),
        @Arg(column = "notice_reason", javaType = String.class)
    })
    LockedNoticeRow getBindingAndNoticeForUpdate(
            @Param("bindingId") String bindingId, @Param("noticeId") String noticeId);

    /** 以 pending 条件原子取得发送资格；返回 1 后才允许在事务外调用 SDK。 */
    @Update(
            "UPDATE channel.notices SET status='sending',started_at=clock_timestamp() WHERE"
                    + " id=#{id} AND status='pending'")
    int updatePendingNoticeSending(String id);

    /** 只更新 pending/sending 的结果。SDK 成功记录 submitted_unverified，失败记录 unknown，均不能伪造送达确认。 */
    @Update(
            "UPDATE channel.notices SET status=#{status},reason=#{reason} WHERE id=#{id} AND status"
                    + " IN ('pending','sending')")
    void updateNoticeOutcome(
            @Param("id") String id, @Param("status") String status, @Param("reason") String reason);

    /** 把失联 sending 标为 unknown，保留外部发送是否发生的不确定性；绝不自动改回 pending 重发。 */
    @Update(
            "UPDATE channel.notices SET status='unknown',reason='worker_lost' WHERE"
                    + " status='sending' AND started_at<clock_timestamp()-interval '60 seconds'")
    void updateExpiredSendingNoticesUnknown();

    @Select(
            "SELECT n.id,n.turn_id,n.binding_id,n.binding_revision,n.revision,NULL AS"
                    + " payload,n.status,n.reason FROM channel.notices n JOIN channel.bindings b ON"
                    + " b.id=n.binding_id WHERE b.tenant=#{tenant} AND b.subject=#{subject} ORDER BY"
                    + " n.created_at DESC LIMIT 100")
    List<Notice> listNoticesByOwner(Owner owner);
}
