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

  @Select("SELECT count(*) FROM business.attempts")
  int attempts();
}
