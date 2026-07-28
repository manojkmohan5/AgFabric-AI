import { redirect } from "next/navigation";

export default function Root() {
  // The shell decides where an unauthenticated visitor lands; this just gets them
  // off "/" so there is no blank route.
  redirect("/dashboard");
}
