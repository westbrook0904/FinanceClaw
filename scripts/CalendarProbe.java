import com.nlf.calendar.Solar;
import java.io.*;
import java.nio.file.*;
import java.time.*;
import java.util.zip.GZIPInputStream;

class CalendarProbe {
 public static void main(String[] args) throws Exception {
  try (var in = new BufferedReader(new InputStreamReader(new GZIPInputStream(Files.newInputStream(Path.of(args[0])))));
       var out = Files.newBufferedWriter(Path.of(args[1]))) {
   int total = 0, mismatches = 0;
   String row;
   while ((row = in.readLine()) != null) {
    String[] values = row.split("\t");var d = LocalDate.parse(values[0]);
    var l = Solar.fromYmd(d.getYear(), d.getMonthValue(), d.getDayOfMonth()).getLunar();
    String actual = d + "\t" + l.getYear() + "\t" + Math.abs(l.getMonth()) + "\t" + l.getDay() + "\t" + (l.getMonth() < 0 ? 1 : 0);
    if (!row.equals(actual)) {mismatches++;out.write(row + " => " + actual + "\n");}
    total++;
   }
   System.out.println("total=" + total + " differences=" + mismatches);
  }
 }
}
