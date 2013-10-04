package com.madmode.py2scala

import scala.collection.{mutable, Iterable}


/**
 * python runtime: builtin functions
 */
object __builtin__ {

  /* types */
  class Dict[K, V] extends mutable.HashMap[K, V] {
    def update(x: Dict[K, V]): Unit = {
      this ++= x
    }

    def get(k: K, default: V) = this.getOrElse(k, default)
  }

  object Dict {
    def apply[K, V]() = Map[K, V]()
  }

  class Instance(o: Object) {
    def __class__ = o.getClass
  }

  trait File extends Iterable[String] {
    def read(): String

    def readline(): String

    def readlines(): Vector[String]

    def write(s: String): Unit

    def flush(): Unit

    def close()
  }


  def TODO = throw new Exception("TODO")

  /* implicits */

  implicit def test_int(i: Int): Boolean = i != 0

  implicit def test_string(s: String): Boolean = s != null && !s.isEmpty

  implicit def test_list[T](s: Seq[T]): Boolean = s != null && !s.isEmpty

  implicit def as_string(c: Char): String = c.toString

  implicit def as_py_char(c: Char): PyChar = new PyChar(c)

  class PyChar(c: Char) {
    def isalpha() = Character.isLetter(c) || Character.isDigit(c)

    def isdigit() = Character.isDigit(c)
  }

  implicit def as_py_string(s: String): PyString = new PyString(s)

  class PyString(s: String) {
    def isalpha(): Boolean = s.forall(_.isalpha())

    def isdigit(): Boolean = s.forall(_.isdigit())

    def strip(): String = s.trim()

    def rstrip(): String = s.replaceAll("\\s+$", "")

    def find(needle: String) = s.indexOf(needle)

    def find(needle: Char) = s.indexOf(needle)

    def startswith(prefix: String) = s.startsWith(prefix)
  }

  implicit def test_dict[K, V](d: Dict[K, V]): Boolean = d != null && !d.isEmpty

  implicit def as_py_instance(o: Object): Instance = new Instance(o)

  implicit def as_py_class[T](cls: java.lang.Class[T]): Class_[T] = new Class_[T](cls)

  class Class_[T](cls: java.lang.Class[T]) {
    def __name__ : String = TODO
  }


  /* file */

  import java.io

  def raw_input(prompt: String): String = {
    val b = new io.BufferedReader(new io.InputStreamReader(System.in))
    System.out.print(prompt)
    b.readLine()
  }

  def open(path: String, mode: String = "r") = new FRFile(
    new io.BufferedReader(new io.FileReader(new io.File(path))))

  def print(target: File, items: Any*) = target.write(items.mkString("", " ", "\n"))
  def print(items: Any*) = println(items.mkString(" "))

  implicit def as_py_file(fp: io.BufferedReader): File = new FRFile(fp)

  implicit def as_py_file(fp: io.PrintStream): File = TODO

  class IOError(msg: String = "") extends Exception(msg)

  class FRFile(fp: io.BufferedReader) extends File {
    def close() = fp.close()

    def read(): String = TODO

    def readline(): String = fp.readLine()

    def readlines(): Vector[String] = {
      import scala.collection.immutable.VectorBuilder
      import scala.annotation.tailrec
      val b = new VectorBuilder[String]
      @tailrec
      def mk(): Unit = {
        val line = fp.readLine()
        if (line == null) ()
        else {
          b += line;
          mk()
        }
      }
      mk()
      b.result()
    }

    def write(s: String) = TODO

    def flush() = TODO

    override def iterator: Iterator[String] = new LinesIterator(fp.readLine())

    class LinesIterator(var line: String) extends Iterator[String] {
      override def hasNext = line != null

      override def next() = {
        val l = line
        line = fp.readLine()
        l
      }
    }

  }


  /* lang */
  def with_[T](obj: T)(blk: (T => Unit)) = {
    blk(obj)
  }


  val True = true
  val False = false

  def range(lo: Int, hi: Int) = lo to hi

  def sum(xs: Iterable[Int]) = xs.reduce(_ + _)

  def int(s: String) = s.toInt

  def len[T](s: Seq[T]) = s.length

  def mod(fmt: String, v: Any*): String = TODO // s % v ; really batteries.operator.mod

  def isinstance[T](o: Object, c: java.lang.Class[T]) = c.isInstance(o)

  def enumerate[A](x: Iterable[A]): Iterable[(Int, A)] = x.zipWithIndex.map(_.swap)

  class NotImplementedError(msg: String) extends Exception(msg)

  class SystemExit extends Exception("SystemExit")
}
