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

    @Select(
            "SELECT b.id FROM channel.bindings b JOIN business.turns t ON t.id=b.active_turn WHERE"
                    + " NOT b.muted AND t.status IN"
                    + " ('waiting','blocked','completed','failed','cancelled') AND NOT EXISTS(SELECT 1"
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

    /** 只观察最早 pending 通知；真正领取还需在事务内锁 binding、notice 并重验版本。 */
    @Select(
            "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason FROM"
                    + " channel.notices WHERE status='pending' ORDER BY created_at LIMIT 1")
    Notice getNextPendingNotice();

    @Select(
            "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason FROM"
                    + " channel.notices WHERE id=#{id} FOR UPDATE")
    Notice getNoticeForUpdate(String id);

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
