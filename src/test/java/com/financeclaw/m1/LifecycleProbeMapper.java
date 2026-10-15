package com.financeclaw.m1;

import org.apache.ibatis.annotations.*;

public interface LifecycleProbeMapper {
  @Update("TRUNCATE business.conversations CASCADE")
  void reset();

  @Update(
      "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE status='running'")
  void expire();

  @Select("SELECT count(*) FROM business.commands")
  int commands();

  @Select("SELECT count(*) FROM business.journal WHERE role='assistant'")
  int answers();

  @Select("SELECT count(*) FROM business.outbox")
  int outbox();

  @Select("SELECT count(*) FROM business.projections")
  int projections();

  @Select("SELECT count(*) FROM business.attempts")
  int attempts();

  @Select("SELECT count(*) FROM business.outbox WHERE NOT delivered")
  int pending();

  @Select("SELECT status FROM business.projections WHERE turn_id=#{id}")
  String projection(String id);
}
